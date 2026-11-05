import time
from multiprocessing import shared_memory, Value
import numpy as np

class CircularBuffer(object):
    def __init__(self, name, capacity, item_size, is_creating,write_count=None):
        # write_count est un pointeur qui pointe vers le dernier élément écrit dans le buffer,
        # Si le buffer est créé, le pointeur sera créé avec, dans son propre espace mémoire partagée
        # Si on se subscribe au buffer, il faudra recevoir le pointeur en externe
        self.name = name
        self.length = capacity                          # La taille du buffer en slot
        self.size_in_bytes = self.length * item_size    # La quantité de mémoire à allouer au buffer en entier (nombre de slot x taille d'un slot)
        self.item_size = item_size                      # La taille en bytes des objets

        if is_creating:
            self.buffer = shared_memory.SharedMemory(name=name, create=True, size=self.size_in_bytes)
            self.write_count = Value('Q', 0, lock=False)

        else:
            # Dans l'idée, l'objet Circular Buffer sera donné à chaque procédé, qui en crééra une copie (de l'entité) mais pas de la donnée mémoire (mémoire partagée)
            # Si on veut un procédé qui se lancerait sans hériter du CB créé dans le main on a besoin de cette partie
            self.buffer = shared_memory.SharedMemory(name=name, create=False)
            if write_count is None:
                raise ValueError("write_count doit être fourni : pas d'attache par nom possible pour Value")
            self.write_count = write_count

        self._view = np.ndarray(
                (self.length, self.item_size),
                dtype=np.uint8,
                buffer=self.buffer.buf
            )

        self._cursors = {}  # dict Python classique, local au process, pour la lecture en parallèle en FIFO du buffer

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_view']  # jamais transporté tel quel
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._view = np.ndarray(  # reconstruit à partir du buffer déjà bien reconnecté
            (self.length, self.item_size),
            dtype=np.uint8,
            buffer=self.buffer.buf
        )
        # ndarray est une feature de numpy pour lui dire "va dans cette espace mémoire, et découpe tout en morceau de X bytes
        # ce qui permet d'avoir "une liste numpy" à partir d'une shared memory, PAS UNE COPIE DU SHM
        # Quand le self._view est héritée par les procédés enfant, les enfants hérite d'une copie du tableau à au moment de l'héritage et pas du tableau dynamique

    def write(self,data):
        idx = self.write_count.value % self.length          # Calcul du slot dans lequel écrire
        self._write_slot(idx, data)                         # Ecriture
        self.write_count.value += 1                         # incrémente, ok parce qu'on a un qu'un seul producteur donc jamais de clash à l'écriture

    def read_latest(self):
        current = self.write_count.value
        if current == 0:
            return None  # rien n'a encore été écrit
        idx = (current - 1) % self.length                   # dernier slot rempli
        return self._read_slot(idx), current

    def _write_slot(self, idx, data: np.ndarray):
        raw = data.reshape(-1).view(np.uint8)  # aplati, vu comme octets bruts
        if raw.nbytes != self.item_size:
            raise ValueError(f"data {raw.nbytes}o != slot {self.item_size}o")
        self._view[idx, :] = raw  # écriture dans la shared memory directement

    def _read_slot(self, idx) -> np.ndarray:
        return self._view[idx, :].copy()  # .copy() = pour éviter de lire une donnée à moitié mise à jour

    def read_next_fifo(self, cursor_id):
        last_seen = self._cursors.setdefault(cursor_id, 0)                  # prend l'id du dernier item vu dans le dico associés au cursor id (donc on peut lire en // le buffer en fifo)
        current = self.write_count.value

        if current <= last_seen:
            return None  # rien de neuf, compteurs égaux, pas de bug ici

        lag = current - last_seen
        gap = 0
        if lag > self.length:
            gap = lag - self.length  # nombre de slots ratés, perdus pour de bon
            last_seen = current - self.length  # on rattrape au plus vieux slot encore valide

        idx = last_seen % self.length
        data = self._read_slot(idx)
        self._cursors[cursor_id] = last_seen + 1
        return data, last_seen, gap

    def close(self):
        self.buffer.close()  # détache ce process du shm, ne détruit rien tant que d'autres l'utilisent

    def unlink(self):
        self.buffer.unlink()  # ne devrait être appelé QUE par le créateur, une fois que tout le monde a fait close()

    def read_last_n(self, n):
        """Snapshot LIFO en bloc : jusqu'à n derniers slots écrits,
        triés du plus vieux au plus récent (ordre chronologique)."""
        before = self.write_count.value
        if before == 0:
            return np.empty((0, self.item_size), dtype=np.uint8), 0

        n = min(n, before, self.length)
        start = before - n
        idxs = [(start + i) % self.length for i in range(n)]
        data = self._view[idxs, :].copy()

        after = self.write_count.value
        overrun = max(0, (after - before) - (self.length - n))
        # overrun > 0 : le writer a tourné pendant la copie, les slots les plus
        # anciens du batch ont pu être partiellement écrasés (impact réel faible ici :
        # on ne s'intéresse qu'aux derniers timestamps, pas aux plus vieux)
        return data, overrun