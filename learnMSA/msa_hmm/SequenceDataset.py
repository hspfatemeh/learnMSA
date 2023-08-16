import numpy as np
import copy
from Bio import SeqIO
import re
from multiprocessing import Pool
from functools import partial


class SequenceDataset:
    """ Contains a set of sequences and their corresponding labels.
    Args:
        filename: Path to a sequence file in any supported format.
        fmt: Format of the file. Can be any format supported by Biopython's SeqIO.
        indexed: If True, Biopython's index method is used to avoid loading the whole file into memory at once. Otherwise 
                 regular parsing is used. Setting this to True will allow constant memory training at the cost of per-step performance.
    """
    #alphabet[:20] corresponds to the traditional aminoacid alphabet
    alphabet = "ARNDCQEGHILKMFPSTWYVXUO-"
    


    def __init__(self, filename, fmt, indexed=False):
        self.filename = filename
        self.fmt = fmt
        self.indexed = indexed
        if indexed:
            self.record_dict = SeqIO.index(filename, fmt)
        else:
            self.record_dict = SeqIO.to_dict(SeqIO.parse(filename, fmt))
        self.seq_ids = list(self.record_dict)
        self.seq_lens = np.array([len(self.record_dict[sid]) for sid in self.seq_ids], dtype=np.int32)
        self.max_len = np.amax(self.seq_lens)
        self.num_seq = len(self.seq_ids)


    def get_record(self, i):
        return self.record_dict[self.seq_ids[i]]


    def get_alphabet_no_gap(self):
        return type(self).alphabet[:-1]


    def get_encoded_seq(self, i, remove_gaps=True, gap_symbols="-.", ignore_symbols="", replace_with_x = "BZJ", validate_alphabet=True, dtype=np.int16):
        seq_str = str(self.get_record(i).upper().seq)
        # replace non-standard aminoacids with X
        for aa in replace_with_x:
            seq_str = seq_str.replace(aa, 'X')
        if remove_gaps:
            for s in gap_symbols:
                seq_str = seq_str.replace(s, '')
        else:
            # unify gap symbols
            for s in gap_symbols:
                seq_str = seq_str.replace(s, gap_symbols[0])
        # strip other symbols
        for s in ignore_symbols:
            seq_str = seq_str.replace(s, '')
        # make sure the sequences do not contain any other symbols
        if validate_alphabet:
            if bool(re.compile(rf"[^{type(self).alphabet}]").search(seq_str)):
                raise ValueError(f"Found unknown character(s) in sequence {self.seq_ids[i]}. Allowed alphabet: {type(self).alphabet}.")
        return np.array([type(self).alphabet.index(aa) for aa in seq_str], dtype=dtype)
     
        
    def validate_dataset(self, single_seq_ok=False, empty_seq_id_ok=False, dublicate_seq_id_ok=False):
        """ Raise an error if something unexpected is found in the sequences. """
        if len(self.seq_ids) == 1 and not single_seq_ok:
            raise ValueError(f"File {self.filename} contains only a single sequence.") 
            
        if len(self.seq_ids) == 0:
            raise ValueError(f"Could not parse any sequences from {self.filename}.") 
        
        if not empty_seq_id_ok:
            for sid in self.seq_ids:
                if sid == '':
                    raise ValueError(f"File {self.filename} contains an empty sequence ID, which is not allowed.") 
        if len(self.seq_ids) > len(set(self.seq_ids)) and not dublicate_seq_id_ok:
            raise ValueError(f"File {self.filename} contains duplicated sequence IDs. learnMSA requires unique sequence IDs.") 


    def __del__(self):
        if self.indexed:
            self.record_dict.close()


class AlignedDataset(SequenceDataset):
    """ A sequence dataset with MSA metadata.
    Args:
        See SequenceDataset.
        threads: Number of threads to use for writing the MSA matrix.
    """
    def __init__(self, filename, fmt, indexed=False, threads=None):
        super().__init__(filename, fmt, indexed)
        self.validate_dataset()
        self.msa_matrix = self._get_msa_matrix(threads)
        # compute a mapping from sequence positions to MSA-column index
        cumsum = np.cumsum(self.msa_matrix != type(self).alphabet.index('-')-1, axis=1)  #A-B--C -> 112223
        diff = np.diff(np.insert(cumsum, 0, 0.0, axis=1), axis=1) #112223 -> 0112223 -> [[(i+1) - i]] -> 101001
        diff_where = [np.argwhere(diff[i,:]).flatten() for i in range(diff.shape[0])]
        self.column_map = np.concatenate(diff_where).flatten()
        self.starting_pos = np.cumsum(self.seq_lens)
        self.starting_pos[1:] = self.starting_pos[:-1]
        self.starting_pos[0] = 0
        self.alignment_len = self.msa_matrix.shape[1]


    def validate_dataset(self):
        super().validate_dataset(single_seq_ok=False, empty_seq_id_ok=False, dublicate_seq_id_ok=False)
        if np.any(self.seq_lens != self.seq_lens[0]):
            raise ValueError(f"File {self.filename} contains sequences of different lengths.")


    def get_column_map(self, i):
        s = self.starting_pos[i]
        e = s + self.seq_lens[i]
        return self.column_map[s:e]

    
    def _get_msa_matrix(self, threads, dtype=np.int16):
        msa_matrix = np.zeros((self.num_seq, self.seq_lens[0]), dtype=dtype)
        with Pool(threads) as p:
            p.map(partial(_write_row, self, msa_matrix, dtype), range(self.num_seq))
        return msa_matrix

def _write_row(data, m, dtype, i):
    m[i,:] = data.get_encoded_seq(i, remove_gaps=False, dtype=dtype)