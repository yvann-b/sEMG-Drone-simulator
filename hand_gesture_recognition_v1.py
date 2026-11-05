from __future__ import annotations

import sys
import time
import json
import joblib
import numpy as np
import pandas as pd
import yaml
from pathlib import Path
from datetime import datetime
from typing import Any
from scipy.signal import butter, iirnotch, sosfilt, tf2sos, welch

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QTextEdit, QProgressBar)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

from pylsl import StreamInlet, resolve_byprop
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score
from sklearn.model_selection import ParameterGrid
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

# Modèle CNN-LSTM personnalisé (optionnel si présent dans le projet)
try:
    from cnn_lstm import CompactCNNLSTM
    HAS_CNN_LSTM = True
except ImportError:
    HAS_CNN_LSTM = False

# Classes pour la reconnaissance de gestes du bras
LABELS = ("geste1", "geste2", "geste3", "geste4", "geste5", "repos")

# ==============================================================================
# 1. TRAITEMENT DU SIGNAL & EXTRACTION DE FEATURES
# ==============================================================================

def filter_signal(
    samples_mv: np.ndarray,
    srate: float,
    bandpass_hz: tuple[float, float] = (20.0, 450.0),
    order: int = 4,
    notch_hz: float | None = 50.0,
) -> np.ndarray:
    low, high = map(float, bandpass_hz)
    nyquist = srate / 2.0
    filtered = sosfilt(
        butter(order, (low, high), btype="bandpass", fs=srate, output="sos"),
        samples_mv,
        axis=0,
    )
    if notch_hz is not None and 0 < notch_hz < nyquist:
        b, a = iirnotch(notch_hz, Q=30.0, fs=srate)
        filtered = sosfilt(tf2sos(b, a), filtered, axis=0)
    return filtered


def fill_missing_locf(arr: np.ndarray) -> np.ndarray:
    """Remplace les NaN et Inf par LOCF (Last Observation Carried Forward)."""
    arr = arr.copy()
    arr[~np.isfinite(arr)] = np.nan
    df = pd.DataFrame(arr)
    df = df.ffill().bfill()
    filled = df.to_numpy()
    return np.nan_to_num(filled, nan=0.0, posinf=0.0, neginf=0.0)


def fix_window_length(window: np.ndarray, target_samples: int) -> np.ndarray:
    """Ajuste la taille de la fenêtre temporelle et nettoie les données."""
    cleaned_window = fill_missing_locf(window)
    n_samples, n_channels = cleaned_window.shape

    if n_samples >= target_samples:
        return cleaned_window[:target_samples, :]

    padding_shape = (target_samples - n_samples, n_channels)
    padded_window = np.vstack([cleaned_window, np.full(padding_shape, np.nan)])
    return fill_missing_locf(padded_window)


def extract_time_features(values: np.ndarray) -> tuple[list[float], tuple[str, ...]]:
    rms = float(np.sqrt(np.mean(values**2)))
    mav = float(np.mean(np.abs(values)))
    waveform_length = float(np.mean(np.abs(np.diff(values))))
    threshold = max(1e-12, 0.01 * rms)
    crossings = np.sum(
        ((values[:-1] >= 0) != (values[1:] >= 0))
        & (np.abs(values[1:] - values[:-1]) >= threshold)
    )
    differences = np.diff(values)
    slope_changes = np.sum(
        ((differences[:-1] >= 0) != (differences[1:] >= 0))
        & (np.abs(differences[1:] - differences[:-1]) >= threshold)
    )
    return [rms, mav, waveform_length, float(crossings), float(slope_changes)], (
        "rms", "mav", "waveform_length", "zero_crossings", "slope_sign_changes"
    )


def extract_features_from_window(
    samples_mv: np.ndarray,
    sampling_rate: float,
    bin_width_s: float,
    channel_names: tuple[str, ...] = ("Ch1", "Ch2", "Ch3", "Ch4"),
) -> tuple[np.ndarray, list[str]]:
    if np.isnan(samples_mv).any():
        samples_mv = fill_missing_locf(samples_mv)

    bin_samples = max(2, int(round(float(bin_width_s) * sampling_rate)))
    total_samples = (samples_mv.shape[0] // bin_samples) * bin_samples
    if total_samples > 0:
        samples_mv = samples_mv[:total_samples, :]

    row: list[float] = []
    names: list[str] = []
    bin_count = samples_mv.shape[0] // bin_samples

    for channel in range(samples_mv.shape[1]):
        ch_name = channel_names[channel] if channel < len(channel_names) else f"Ch{channel+1}"
        for bin_index in range(bin_count):
            start = bin_index * bin_samples
            stop = start + bin_samples
            values, local_names = extract_time_features(samples_mv[start:stop, channel])
            row.extend(values)
            if len(names) < len(row):
                names.extend(f"{ch_name}__bin{bin_index + 1:02d}__{n}" for n in local_names)

        frequencies, power = welch(
            samples_mv[:, channel],
            fs=sampling_rate,
            nperseg=min(512, len(samples_mv)),
        )
        positive_power = np.maximum(power, 0)
        total_power = float(np.sum(positive_power))
        if total_power <= 0:
            mean_freq = median_freq = 0.0
        else:
            mean_freq = float(np.sum(frequencies * positive_power) / total_power)
            median_freq = float(frequencies[np.searchsorted(np.cumsum(positive_power), total_power / 2)])
        row.extend([mean_freq, median_freq])
        if len(names) < len(row):
            names.extend([f"{ch_name}__mean_frequency", f"{ch_name}__median_frequency"])

    return np.array(row, dtype=float), names


# ==============================================================================
# 2. SELECTION DE MODÈLES & COMPARAISON (GROUPED CV)
# ==============================================================================

def model_candidates() -> dict[str, tuple[Any, dict[str, list[Any]]]]:
    models = {
        "lda_shrinkage": (
            Pipeline([("scale", StandardScaler()), ("model", LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto"))]),
            {},
        ),
        "logistic": (
            Pipeline([("scale", StandardScaler()), ("model", LogisticRegression(class_weight="balanced", max_iter=5000, random_state=2026))]),
            {"model__C": [0.1, 1.0, 10.0]},
        ),
        "svm_rbf": (
            Pipeline([("scale", StandardScaler()), ("model", CalibratedClassifierCV(estimator=SVC(kernel="rbf", class_weight="balanced", random_state=2026), method="sigmoid", cv=3))]),
            {"model__estimator__C": [0.1, 1.0, 10.0], "model__estimator__gamma": ["scale", 0.01]},
        ),
        "random_forest": (
            Pipeline([("model", RandomForestClassifier(n_estimators=200, max_features="sqrt", class_weight="balanced", random_state=2026, n_jobs=-1))]),
            {"model__max_depth": [3, 5, None], "model__min_samples_leaf": [1, 2]},
        ),
    }
    
    """if HAS_CNN_LSTM:
        models["cnn_lstm"] = (
            CompactCNNLSTM(channels=4, epochs=25, batch_size=16, learning_rate=0.001, random_state=2026),
            {"epochs": [20, 30], "learning_rate": [0.001]}
        )"""
    return models


def select_model_grouped_cv_live(
    X: np.ndarray, 
    y: np.ndarray, 
    blocks: np.ndarray, 
    candidate_bin_widths_s: list[float], 
    srate: float, 
    raw_windows: list[np.ndarray]
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    results = []
    best = None
    best_oof = None

    X_raw = np.array([w.reshape(-1) for w in raw_windows], dtype=np.float32)
    unique_blocks = np.unique(blocks)

    for bin_width in candidate_bin_widths_s:
        X_feats = []
        feature_names = []
        for w in raw_windows:
            feats, names = extract_features_from_window(w, srate, bin_width)
            X_feats.append(feats)
            feature_names = names
        X_mat = np.array(X_feats)

        for model_name, (pipeline, grid) in model_candidates().items():
            if model_name == "cnn_lstm" and bin_width != candidate_bin_widths_s[0]:
                continue

            X_input = X_raw if model_name == "cnn_lstm" else X_mat
            parameters = list(ParameterGrid(grid)) if grid else [{}]

            for params in parameters:
                prediction = np.empty(len(y), dtype=object)
                probabilities = np.zeros((len(y), len(LABELS)), dtype=float)
                fold_acc, fold_scores, fold_f1 = [], [], []

                for held_out in unique_blocks:
                    val_mask = blocks == held_out
                    train_mask = ~val_mask

                    fitted = clone(pipeline).set_params(**params).fit(X_input[train_mask], y[train_mask])
                    fold_pred = fitted.predict(X_input[val_mask])
                    fold_prob = fitted.predict_proba(X_input[val_mask])
                    prediction[val_mask] = fold_pred

                    for idx, lbl in enumerate(fitted.classes_):
                        if lbl in LABELS:
                            probabilities[val_mask, LABELS.index(lbl)] = fold_prob[:, idx]

                    fold_acc.append(accuracy_score(y[val_mask], fold_pred))
                    fold_scores.append(balanced_accuracy_score(y[val_mask], fold_pred))
                    fold_f1.append(f1_score(y[val_mask], fold_pred, average="macro"))

                cv_acc = float(np.mean(fold_acc))
                cv_score = float(np.mean(fold_scores))
                row = {
                    "bin_width_s": float(bin_width) if model_name != "cnn_lstm" else None,
                    "model": model_name,
                    "params": params,
                    "cv_accuracy_mean": cv_acc,
                    "cv_balanced_accuracy_mean": cv_score,
                    "cv_macro_f1_mean": float(np.mean(fold_f1)),
                }
                results.append(row)

                if best is None or cv_acc > best["cv_accuracy_mean"]:
                    oof_df = pd.DataFrame({
                        "true_label": y,
                        "predicted_label": prediction,
                        "confidence": np.max(probabilities, axis=1)
                    })
                    best = {
                        **row, 
                        "feature_names": feature_names if model_name != "cnn_lstm" else [], 
                        "X_train_data": X_input
                    }
                    best_oof = oof_df

    cv_table = pd.DataFrame(results).sort_values("cv_accuracy_mean", ascending=False).reset_index(drop=True)
    return best, cv_table, best_oof


# ==============================================================================
# 3. MOTEUR D'ACQUISITION & EXÉCUTION (EMG ARM RUNNER)
# ==============================================================================

class ArmEMGRunner:
    def __init__(self, config: dict, status_callback=None, ui_callback=None):
        self.config = config
        self.status_cb = status_callback or print
        self.ui_cb = ui_callback or (lambda e: None)
        self.inlets = []
        self.fs = config['acquisition']['sampling_rate_expected_hz']
        self.bundle = None
        self.is_running = False

    def connect_lsl(self):
        stream_names = self.config['acquisition'].get('lsl_stream_names', ["Shimmer_EMG_Left", "Shimmer_EMG_Right"])
        self.inlets = []
        for name in stream_names:
            self.status_cb(f"Recherche du flux LSL {name}...")
            streams = resolve_byprop('name', name, timeout=5)
            if not streams:
                raise RuntimeError(f"Flux introuvable: {name}")
            self.inlets.append(StreamInlet(streams[0]))
            self.status_cb(f"Connecté à {name}")

    def flush_buffers(self):
        for inlet in self.inlets:
            if inlet is not None:
                while inlet.pull_sample(timeout=0.0)[0] is not None:
                    pass

    # Remplacer la boucle de capture dans record_epoch par :
    def record_epoch(self, duration_sec: float) -> np.ndarray:
        self.flush_buffers()
        samples = []
        start_time = time.time()
        while (time.time() - start_time) < duration_sec:
            # On extrait 2 canaux de chaque Shimmer
            sample_s1, _ = self.inlets[0].pull_sample(timeout=0.01)
            sample_s2, _ = self.inlets[1].pull_sample(timeout=0.01) if len(self.inlets) > 1 else (None, None)
            
            if sample_s1 is not None and sample_s2 is not None:
                # Concaténation : 2 canaux du Shimmer 1 + 2 canaux du Shimmer 2
                combined_sample = sample_s1[:2] + sample_s2[:2]
                samples.append(combined_sample)
                
        if not samples:
            return np.array([])
        return np.array(samples)

    def save_dataset_local(self, raw_windows: list[np.ndarray], labels: list[str], blocks: list[int]):
        """Sauvegarde du dataset en local au format .npz."""
        save_dir = Path("./datasets")
        save_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = save_dir / f"emg_arm_dataset_15perclass_{timestamp}.npz"
        
        X_array = np.array(raw_windows, dtype=np.float32)
        y_array = np.array(labels)
        blocks_array = np.array(blocks)

        np.savez_compressed(
            filename,
            X=X_array,
            y=y_array,
            blocks=blocks_array,
            sampling_rate=self.fs,
            labels_map=LABELS
        )
        self.status_cb(f"Dataset sauvegardé en local : {filename} ({len(y_array)} essais enregistrés)")

    def run_training_phase(self):
        if not self.inlets:
            self.connect_lsl()

        acq_cfg = self.config['acquisition']
        analysis_cfg = self.config['analysis']
        
        # 15 échantillons par classe répartis sur 3 blocs (5 par classe/bloc)
        num_blocks = acq_cfg.get('num_blocks', 3)
        samples_per_block_per_class = acq_cfg.get('samples_per_block_per_class', 5)
        
        response_dur = acq_cfg['response_window_s']
        rest_dur = acq_cfg.get('rest_duration_s', 2.0)
        pause_between_blocks_s = acq_cfg.get('pause_between_blocks_s', 10.0)

        trials_plan = []
        for block in range(num_blocks):
            block_plan = list(LABELS) * samples_per_block_per_class
            np.random.shuffle(block_plan)
            for label in block_plan:
                trials_plan.append((block, label))

        raw_windows, labels, blocks = [], [], []
        self.is_running = True
        total_trials = len(trials_plan)
        self.status_cb(f"Début de la collecte sEMG Bras ({total_trials} essais au total, 15 par classe)...")

        current_block = -1
        for i, (block, label) in enumerate(trials_plan):
            if not self.is_running:
                return

            if block != current_block and current_block != -1:
                self.status_cb(f"Pause musculaire de {pause_between_blocks_s}s avant le bloc {block+1}/{num_blocks}...")
                for pause_left in range(int(pause_between_blocks_s), 0, -1):
                    if not self.is_running:
                        return
                    self.ui_cb({'type': 'CUE', 'text': f"PAUSE BLOC {block+1}: {pause_left}s", 'color': '#333333'})
                    time.sleep(1.0)

            current_block = block

            color = '#1b5e20' if 'geste' in label else '#212121'
            self.ui_cb({'type': 'CUE', 'text': f"BLOC {block+1}/{num_blocks} — EFFECTUEZ : {label.upper()}", 'color': color})
            self.status_cb(f"Essai {i+1}/{total_trials} ({label.upper()})")

            target_samples = int(response_dur * self.fs)
            raw_epoch = self.record_epoch(response_dur)
            if len(raw_epoch) > 0:
                raw_epoch = fix_window_length(raw_epoch, target_samples)
                filtered_epoch = filter_signal(
                    raw_epoch,
                    srate=self.fs,
                    bandpass_hz=tuple(analysis_cfg["bandpass_hz"]),
                    notch_hz=analysis_cfg.get("notch_hz", 50.0),
                )
                filtered_epoch = fill_missing_locf(filtered_epoch)

                raw_windows.append(filtered_epoch)
                labels.append(label)
                blocks.append(block)

            self.ui_cb({'type': 'CUE', 'text': 'REPOS', 'color': '#111111'})
            time.sleep(rest_dur)

        # 1. Sauvegarde locale du dataset
        self.save_dataset_local(raw_windows, labels, blocks)

        # 2. Entraînement et sélection par validation croisée
        self.status_cb("Évaluation et sélection du meilleur modèle...")
        X_dummy = np.zeros(len(labels))
        y = np.array(labels)
        blocks_arr = np.array(blocks)

        best, cv_table, oof = select_model_grouped_cv_live(
            X_dummy, y, blocks_arr, analysis_cfg['candidate_bin_widths_s'], self.fs, raw_windows
        )

        # Re-entraînement complet du meilleur modèle retenu
        base, _ = model_candidates()[best['model']]
        estimator = clone(base).set_params(**best['params']).fit(best['X_train_data'], y)

        train_accuracy = accuracy_score(y, estimator.predict(best['X_train_data'])) * 100.0
        cv_accuracy = best['cv_accuracy_mean'] * 100.0

        self.status_cb(f"--- RÉSULTATS COMPARATIFS ---")
        self.status_cb(f"Meilleur modèle sélectionné : {best['model'].upper()}")
        self.status_cb(f"--> Accuracy Moyenne CV: {cv_accuracy:.2f}%")
        self.status_cb(f"--> Accuracy Globale Entraînement: {train_accuracy:.2f}%")

        confidence_threshold = 0.4
        target_far = analysis_cfg.get("maximum_rest_false_activation_rate", 0.05)
        rest_mask = oof["true_label"].to_numpy() == "repos"
        if np.any(rest_mask):
            for th in np.linspace(0.0, 1.0, 101):
                accepted = oof["confidence"].to_numpy() >= th
                pred = oof["predicted_label"].to_numpy()
                far = (rest_mask & accepted & (pred != "repos")).sum() / rest_mask.sum()
                if far <= target_far:
                    confidence_threshold = float(th)
                    break

        self.bundle = {
            "estimator": estimator,
            "model_name": best["model"],
            "bin_width_s": best["bin_width_s"],
            "feature_names": best["feature_names"],
            "confidence_threshold": confidence_threshold,
        }

        self.ui_cb({
            'type': 'TRAINING_COMPLETE',
            'model_name': best['model'],
            'cv_acc': cv_accuracy,
            'train_acc': train_accuracy
        })

    def run_live_phase(self):
        if not self.inlets:
            self.connect_lsl()
        if not self.bundle:
            self.status_cb("Erreur : Aucun modèle n'est prêt pour le live !")
            return

        live_cfg = self.config.get('live', {})
        win_size = self.config['acquisition']['response_window_s']
        step_sec = live_cfg.get('step_sec', 0.1)
        target_len = int(win_size * self.fs)

        buffer_samples = []
        self.is_running = True
        self.status_cb(f"Session LIVE activée avec le modèle : {self.bundle['model_name'].upper()}")

        has_second_inlet = len(self.inlets) > 1

        while self.is_running:
            chunk_s1, _ = self.inlets[0].pull_chunk(timeout=0.02)
            chunk_s2, _ = self.inlets[1].pull_chunk(timeout=0.02) if has_second_inlet else ([], None)

            if has_second_inlet:
                # Cas 2 Shimmers
                if chunk_s1 and chunk_s2:
                    min_len = min(len(chunk_s1), len(chunk_s2))
                    for i in range(min_len):
                        buffer_samples.append(chunk_s1[i][:2] + chunk_s2[i][:2])
            else:
                # Cas 1 Shimmer (4 canaux)
                if chunk_s1:
                    for s in chunk_s1:
                        buffer_samples.append(s[:4])

            if len(buffer_samples) >= target_len:
                raw_win = np.array(buffer_samples[-target_len:])
                filtered_win = filter_signal(
                    raw_win, self.fs, 
                    bandpass_hz=tuple(self.config['analysis']['bandpass_hz']),
                    notch_hz=self.config['analysis'].get('notch_hz', 50.0)
                )
                filtered_win = fill_missing_locf(filtered_win)

                if self.bundle['model_name'] == "cnn_lstm":
                    model_input = filtered_win.reshape(1, -1)
                else:
                    feats, _ = extract_features_from_window(filtered_win, self.fs, self.bundle['bin_width_s'])
                    model_input = feats.reshape(1, -1)

                probs = self.bundle['estimator'].predict_proba(model_input)[0]
                max_idx = np.argmax(probs)
                confidence = float(probs[max_idx])
                pred_label = str(self.bundle['estimator'].classes_[max_idx])

                decision = pred_label if confidence >= self.bundle['confidence_threshold'] else "incertain"

                self.ui_cb({
                    'type': 'LIVE_PREDICTION',
                    'label': decision.upper(),
                    'confidence': confidence,
                })

                discard = int(step_sec * self.fs)
                buffer_samples = buffer_samples[discard:]

            time.sleep(step_sec / 2)

    def stop(self):
        self.is_running = False


# ==============================================================================
# 4. INTERFACE GRAPHIQUE (PYSIDE6)
# ==============================================================================

class ArmEMGWorker(QThread):
    status_signal = Signal(str)
    ui_event_signal = Signal(dict)

    def __init__(self, config_dict: dict):
        super().__init__()
        self.config = config_dict
        self.runner = ArmEMGRunner(
            config=self.config,
            status_callback=self.status_signal.emit,
            ui_callback=self.ui_event_signal.emit
        )
        self.mode = 'train'

    def run(self):
        try:
            if self.mode == 'train':
                self.runner.run_training_phase()
            elif self.mode == 'live':
                self.runner.run_live_phase()
        except Exception as e:
            self.status_signal.emit(f"Erreur d'exécution : {str(e)}")

    def stop(self):
        self.runner.stop()
        self.wait()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("sEMG Arm Gesture Classification & Live Interface")
        self.resize(850, 650)
        
        default_config = {
            'acquisition': {
                'sampling_rate_expected_hz': 512.0,
                'lsl_stream_names': ["Shimmer_EMG_Left", "Shimmer_EMG_Right"],
                'response_window_s': 5.0,
                'rest_duration_s': 3.0,
                'num_blocks': 3,
                'samples_per_block_per_class': 5,
                'pause_between_blocks_s': 10.0
            },
            'analysis': {
                'bandpass_hz': [20.0, 200.0],
                'notch_hz': 50.0,
                'candidate_bin_widths_s': [0.25, 0.5],
                'maximum_rest_false_activation_rate': 0.05
            },
            'live': {
                'step_sec': 0.1
            }
        }

        self.worker = ArmEMGWorker(default_config)
        self.worker.status_signal.connect(self.log_message)
        self.worker.ui_event_signal.connect(self.handle_ui_event)

        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.display_box = QLabel("PRÊT POUR L'ACQUISITION")
        self.display_box.setAlignment(Qt.AlignCenter)
        self.display_box.setFont(QFont("Arial", 24, QFont.Bold))
        self.display_box.setStyleSheet("background-color: #2b2b2b; color: white; border-radius: 12px; padding: 25px;")
        layout.addWidget(self.display_box)

        self.confidence_bar = QProgressBar()
        self.confidence_bar.setRange(0, 100)
        layout.addWidget(self.confidence_bar)

        btn_layout = QHBoxLayout()
        self.btn_train = QPushButton("1. Lancer Collecte & Comparaison Modèles")
        self.btn_train.clicked.connect(self.start_training)

        self.btn_live = QPushButton("2. Démarrer Inférence Live")
        self.btn_live.setEnabled(False)
        self.btn_live.clicked.connect(self.start_live)

        self.btn_stop = QPushButton("Arrêter")
        self.btn_stop.clicked.connect(self.stop_all)

        btn_layout.addWidget(self.btn_train)
        btn_layout.addWidget(self.btn_live)
        btn_layout.addWidget(self.btn_stop)
        layout.addLayout(btn_layout)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet("background-color: #1a1a1a; color: #00ff66; font-family: Monospace;")
        layout.addWidget(self.log_console)

    def log_message(self, msg):
        self.log_console.append(msg)

    def handle_ui_event(self, event):
        t = event.get('type')
        if t == 'CUE':
            self.display_box.setText(event.get('text', ''))
            self.display_box.setStyleSheet(f"background-color: {event.get('color', '#2b2b2b')}; color: white; border-radius: 12px; padding: 25px;")
        elif t == 'TRAINING_COMPLETE':
            model = event.get('model_name', '').upper()
            cv_acc = event.get('cv_acc', 0.0)
            train_acc = event.get('train_acc', 0.0)
            msg = f"ENTRAÎNEMENT TERMINÉ\nMeilleur Modèle: {model}\nAccuracy CV: {cv_acc:.1f}% | Train: {train_acc:.1f}%"
            self.display_box.setText(msg)
            self.display_box.setStyleSheet("background-color: #1b5e20; color: white; border-radius: 12px; padding: 15px;")
            self.btn_live.setEnabled(True)
        elif t == 'LIVE_PREDICTION':
            lbl = event.get('label', '')
            conf = event.get('confidence', 0.0) * 100
            self.display_box.setText(f"GESTE DÉTECTÉ : {lbl}")
            self.confidence_bar.setValue(int(conf))

    def start_training(self):
        self.btn_live.setEnabled(False)
        self.worker.mode = 'train'
        self.worker.start()

    def start_live(self):
        self.worker.mode = 'live'
        self.worker.start()

    def stop_all(self):
        self.worker.stop()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())