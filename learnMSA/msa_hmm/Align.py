import tensorflow as tf
import numpy as np
import time
import os
import learnMSA.msa_hmm.Fasta as fasta
import learnMSA.msa_hmm.Training as train
import learnMSA.msa_hmm.MsaHmmLayer as msa_hmm
import learnMSA.msa_hmm.Viterbi as viterbi
from learnMSA.msa_hmm.Configuration import as_str, assert_config, get_adaptive_batch_size
from pathlib import Path


 
""" Trains k independent models on the sequences in a fasta file and returns k "lazy" alignments, where "lazy" means 
    that decoding will only be carried out when the user wants to print the alignment or write it to a file. 
    Decoding is usually expensive and typically it should only be done after a model selection step.
Args: 
    fasta_file: A Fasta object.
    config: Configuration that can be used to control training and decoding (see msa_hmm.config.make_default).
    model_generator: Optional callback that generates a user defined model (if None, the default model generator will be used). 
    batch_generator: Optional callback that generates sequence batches defined by user (if None, the default batch generator will be used).
    subset: Optional subset of the sequence ids. Only the specified sequences will be aligned but the models will be trained on all sequences 
            (if None, all sequences in the fasta file will be aligned).
    verbose: If False, all output messages will be disabled.
Returns:
    An Alignment object.
"""
def fit_and_align(fasta_file, 
                  config,
                  model_generator=None,
                  batch_generator=None,
                  subset=None,
                  verbose=True):
    assert_config(config)
    model_generator, batch_generator = _make_defaults_if_none(fasta_file, config, model_generator, batch_generator)
    if verbose:
        _fasta_file_messages(fasta_file)
    n = fasta_file.num_seq
    if subset is None:
        subset = np.arange(fasta_file.num_seq)
    full_length_estimate = get_full_length_estimate(fasta_file, config) 
    model_lengths = get_initial_model_lengths(fasta_file, config)
    #model surgery
    last_iteration=config["max_surgery_runs"]==1
    for i in range(config["max_surgery_runs"]):
        if config["batch_size"] == "adaptive":
            batch_size = get_adaptive_batch_size(model_lengths, fasta_file.max_len)
        else:
            batch_size = config["batch_size"]
        #set the batch size to something smaller than the dataset size even though
        #for low sequence numbers it would be feasible to train on all data at once
        batch_size = min(batch_size, int(np.ceil(n*0.5)))
        if last_iteration:    
            train_indices = np.arange(n)
            decode_indices = subset
        else:
            train_indices = full_length_estimate
            decode_indices = full_length_estimate
        epochs_this_iteration = config["epochs"][0 if i==0 else 1 if not last_iteration else 2]
        model, history = train.fit_model(model_generator,
                                          batch_generator,
                                          fasta_file,
                                          train_indices,
                                          model_lengths, 
                                          config,
                                          batch_size=batch_size, 
                                          epochs=epochs_this_iteration,
                                          verbose=verbose)
        alignment = Alignment(fasta_file,
                               batch_generator,
                               decode_indices,
                               batch_size=batch_size, 
                               model=model,
                               build="lazy" if last_iteration else "eager")
        if last_iteration:
            loglik, prior = compute_loglik(alignment)
            alignment.loglik = loglik 
            alignment.prior = prior
            if verbose:
                print("Fitted models with MAP estimates = ", 
                      ",".join("%.4f" % (l + p) for l,p in zip(loglik, prior)))
            break
        if i == 0: # remember the initializers used in the first iteration
            emission_init_0, transition_init_0, flank_init_0 = _get_initializers(alignment)
        surgery_converged = True
        #duplicate the previous emitters and transitioner and replace their initializers later
        config["emitter"] = [em.duplicate() for em in alignment.msa_hmm_layer.cell.emitter]
        config["transitioner"] = alignment.msa_hmm_layer.cell.transitioner.duplicate()
        for k in range(config["num_models"]):
            pos_expand, expansion_lens, pos_discard = get_discard_or_expand_positions(alignment, 
                                                                                      k,
                                                                                        del_t=config["surgery_del"], 
                                                                                        ins_t=config["surgery_ins"],
                                                                                        ins_long=100000, 
                                                                                        k=32, 
                                                                                        match_prior_threshold=1)
            surgery_converged &= pos_expand.size == 0 and pos_discard.size == 0
            if verbose:
                print(f"expansions model {k}:", list(zip(pos_expand, expansion_lens)))
                print(f"discards model {k}:", pos_discard)
            transition_init, emission_init, flank_init = update_kernels(alignment, 
                                                                        k,
                                                                        pos_expand,
                                                                        expansion_lens, 
                                                                        pos_discard,
                                                                        [e[k] for e in emission_init_0], 
                                                                        transition_init_0[k], 
                                                                        flank_init_0[k])
            for em, old_em, e_init in zip(config["emitter"], alignment.msa_hmm_layer.cell.emitter, emission_init):
                em.emission_init[k] = tf.constant_initializer(e_init) 
                em.insertion_init[k] = tf.constant_initializer(old_em.insertion_kernel[k].numpy())
            config["transitioner"].transition_init[k] = {key : tf.constant_initializer(t) 
                                         for key,t in transition_init.items()}
            config["transitioner"].flank_init[k] = tf.constant_initializer(flank_init)
            model_lengths[k] = emission_init[0].shape[0]
            if model_lengths[k] < 3: 
                raise SystemExit("A problem occured during model surgery: A pHMM is too short (length <= 2).") 
        if "encoder_weight_extractor" in config:
            if verbose:
                print("Used the encoder_weight_extractor callback to pass the encoder parameters to the next iteration.")
            config["encoder_initializer"] = config["encoder_weight_extractor"](alignment.encoder_model)
        elif verbose:
            print("Re-initialized the encoder parameters.")
        last_iteration = surgery_converged or (i == config["max_surgery_runs"]-2)
    return alignment


def run_learnMSA(train_filename,
                 out_filename,
                 config, 
                 model_generator=None,
                 batch_generator=None,
                 ref_filename="", 
                 verbose=True, 
                 select_best_for_comparison=True):
    """ Wraps fit_and_align and adds file parsing, verbosity, model selection, reference file comparison and an outfile file.
    Args: 
        train_filename: Path of a fasta file with the sequences. 
        out_filename: Filepath of the output fasta file with the aligned sequences.
        config: Configuration that can be used to control training and decoding (see msa_hmm.config.make_default).
        model_generator: Optional callback that generates a user defined model (if None, the default model generator will be used). 
        batch_generator: Optional callback that generates sequence batches defined by user(if None, the default batch generator will be used).
        ref_filename: Optional filepath to a reference alignment. If given, the computed alignment is scored and 
                        the score is returned along with the alignment.
        verbose: If False, all output messages will be disabled.
        select_best_for_comparison: If False, all trained models, not just the one with highest score, will be scored.
    Returns:
        An Alignment object.
    """
    if verbose:
        print("Training of", config["num_models"], " models on file", os.path.basename(train_filename))
        print("Configuration:")
        print(as_str(config))
    # load the file
    fasta_file = fasta.Fasta(train_filename)  
    # optionally load the reference and find the corresponding sequences in the train file
    if ref_filename != "":
        ref_fasta = fasta.Fasta(ref_filename, aligned=True)
        subset = np.array([fasta_file.seq_ids.index(sid) for sid in ref_fasta.seq_ids])
    else:
        subset = None
    try:
        t_a = time.time()
        alignment = fit_and_align(fasta_file, 
                                  config=config,
                                  model_generator=model_generator,
                                  subset=subset, 
                                  verbose=verbose)
        if verbose:
            print("Time for alignment:", "%.4f" % (time.time()-t_a))
    except tf.errors.ResourceExhaustedError as e:
        print("Out of memory. A resource was exhausted.")
        print("Try reducing the batch size (-b). The current batch size was: "+str(config["batch_size"])+".")
        sys.exit(e.error_code)
        
    alignment.best_model = np.argmax(alignment.loglik + alignment.prior)
    if verbose:
        likelihoods = ["%.4f" % ll + " (%.4f)" % p for ll,p in zip(alignment.loglik, alignment.prior)]
        print("Computed alignments with likelihoods (priors): ", likelihoods)
        print("Best model: ", alignment.best_model)
        
    Path(os.path.dirname(out_filename)).mkdir(parents=True, exist_ok=True)
    t = time.time()
    alignment.to_file(out_filename, alignment.best_model)
    
    if verbose:
        print("time for generating output:", "%.4f" % (time.time()-t))
        print("Wrote file", out_filename)

    if ref_filename != "":
        if select_best_for_comparison:
            out_file = fasta.Fasta(out_filename, aligned=True) 
            _,sp = out_file.precision_recall(ref_fasta)
            #tc = out_file.tc_score(ref_fasta)
            if verbose:
                print("SP score =", sp)
        else:
            for i in range(alignment.msa_hmm_layer.cell.num_models):
                tmp_file = "tmp.fasta"
                alignment.to_file(tmp_file, i)
                tmp_fasta = fasta.Fasta(tmp_file, aligned=True) 
                _,sp = tmp_fasta.precision_recall(ref_fasta)
                print(f"Model {i} SP score =", sp)
                os.remove(tmp_file)
        return alignment, sp
    else:
        return alignment
    

def decode_core(model_length,
                state_seqs_max_lik,
                indices):
    """ Decodes consensus columns as a matrix as well as insertion lengths and starting positions
        as auxiliary vectors.
    Args: 
        model_length: Number of match states (length of the consensus sequence).
        state_seqs_max_lik: A tensor with the most likeli state sequences. Shape: (num_seq, L)
        indices: Indices in the sequences where decoding should start. Shape: (num_seq)
    Returns:
        consensus_columns: Decoded consensus columns. Shape: (num_seq, model_length)
        insertion_lens: Number of amino acids emitted per insertion state. Shape: (num_seq, model_length-1)
        insertion_start: Starting position of each insertion in the sequences. Shape: (num_seq, model_length-1)
        finished: Boolean vector indicating sequences that are fully decoded. Shape: (num_seq) 
    """
    n = state_seqs_max_lik.shape[0]
    c = model_length 
    #initialize the consensus with gaps
    consensus_columns = -np.ones((n, c), dtype=np.int16) 
    #insertion lengths and starting positions per sequence
    insertion_lens = np.zeros((n, c-1), dtype=np.int16)
    insertion_start = -np.ones((n, c-1), dtype=np.int16)
    #is true if and only if the previous hidden state was an insertion state (not counting flanks)
    last_insert = np.zeros(n, dtype=bool)
    A = np.arange(n)
    while True:
        q = state_seqs_max_lik[A, indices] 
        is_match = ((q > 0) & (q < c+1))
        is_insert = ((q >= c+1) & (q < 2*c))
        is_insert_start = is_insert & ~last_insert
        is_unannotated = (q == 2*c)
        is_at_end = ((q == 2*c+1) | (q == 2*c+2))
        if np.all(is_unannotated | is_at_end):
            finished = ~is_unannotated
            break
        # track matches
        consensus_columns[A[is_match], q[is_match]-1] = indices[is_match]
        # track insertions
        is_insert_subset = A[is_insert]
        is_insert_start_subset = A[is_insert_start]
        insertion_lens[is_insert_subset, q[is_insert]-c-1] += 1
        insertion_start[is_insert_start_subset, q[is_insert_start]-c-1] = indices[is_insert_start]
        indices[is_match | is_insert] += 1
        last_insert = is_insert
    return consensus_columns, insertion_lens, insertion_start, finished


def decode_flank(state_seqs_max_lik, 
                 flank_state_id, 
                 indices):
    """ Decodes flanking insertion states. The deconding is active as long as at least one sequence remains 
        in a flank/unannotated state.
    Args: 
        state_seqs_max_lik: A tensor with the most likeli state sequences. Shape: (num_seq, L)
        flank_state_id: Index of the flanking state.
        indices: Indices in the sequences where decoding should start. Shape: (num_seq)
    Returns:
        insertion_lens: Number of amino acids emitted per insertion state. Shape: (num_seq, model_length-1)
        insertion_start: Starting position of each insertion in the sequences. Shape: (num_seq, model_length-1)
    """
    n = state_seqs_max_lik.shape[0]
    insertion_start = np.copy(indices)
    while True:
        q = state_seqs_max_lik[np.arange(n), indices]
        is_flank = (q == flank_state_id)
        if ~np.any(is_flank):
            break
        indices[is_flank] += 1
    insertion_lens = indices - insertion_start
    return insertion_lens, insertion_start



def decode(model_length, state_seqs_max_lik):
    """ Decodes an implicit alignment (insertion start/length are represented as 2 integers) 
        from most likely state sequences.
    Args: 
        model_length: Number of match states (length of the consensus sequence).
        state_seqs_max_lik: A tensor with the most likeli state sequences. Shape: (num_seq, L)
    Returns:
        core_blocks: Representation of the consensus. 
        left_flank:
        right_flank:
        unannotated_segments:
    """
    n = state_seqs_max_lik.shape[0]
    c = model_length #alias for code readability
    indices = np.zeros(n, np.int16) # active positions in the sequence
    left_flank = decode_flank(state_seqs_max_lik, 0, indices) 
    core_blocks = []
    unannotated_segments = []
    while True:    
        C, IL, IS, finished = decode_core(model_length, state_seqs_max_lik, indices)
        core_blocks.append((C, IL, IS, finished))
        if np.all(finished):
            break
        unannotated_segments.append( decode_flank(state_seqs_max_lik, 2*c, indices) )
    right_flank = decode_flank(state_seqs_max_lik, 2*c+1, indices) 
    return core_blocks, left_flank, right_flank, unannotated_segments


def get_insertion_block(sequences, 
                        lens, 
                        maxlen,
                        starts,
                        align_to_right=False):
    """ Constructs one insertion block from an implicitly represented alignment.
    Args: 
    Returns:
    """
    A = np.arange(sequences.shape[0])
    block = np.zeros((sequences.shape[0], maxlen), dtype=np.uint8) + (fasta.s-1)
    lens = np.copy(lens)
    active = lens > 0
    i = 0
    while np.any(active):
        aa = sequences[A[active], starts[active] + i]
        block[active, i] = aa
        lens -= 1
        active = lens > 0
        i += 1
    if align_to_right:
        block_right_aligned = np.zeros_like(block) + (fasta.s-1)
        for i in range(maxlen):
            block_right_aligned[A, (maxlen-lens+i)%maxlen] = block[:, i]
        block = block_right_aligned
    block += fasta.s #lower case
    return block
    

def get_alignment_block(sequences, 
                        consensus, 
                        ins_len, 
                        ins_len_total,
                        ins_start):
    """ Constructs one core model hit block from an implicitly represented alignment.
    Args: 
    Returns:
    """
    A = np.arange(sequences.shape[0])
    length = consensus.shape[1] + np.sum(ins_len_total)
    block = np.zeros((sequences.shape[0], length), dtype=np.uint8) + (fasta.s-1)
    i = 0
    for c in range(consensus.shape[1]-1):
        column = consensus[:,c]
        ins_l = ins_len[:,c]
        ins_l_total = ins_len_total[c]
        ins_s = ins_start[:,c]
        #one column
        no_gap = column != -1
        block[no_gap,i] = sequences[A[no_gap],column[no_gap]]
        i += 1
        #insertion
        block[:,i:i+ins_l_total] = get_insertion_block(sequences,
                                                       ins_l,
                                                       ins_l_total, 
                                                       ins_s)
        i += ins_l_total
    #final column
    no_gap = consensus[:,-1] != -1
    block[no_gap,i] = sequences[A[no_gap],consensus[:,-1][no_gap]]
    return block




class Alignment():
    """ Decodes alignments from a number of models, stores them in a memory friendly representation and
        generates table-form (memory unfriendly) alignments on demand (batch-wise mode possible).
    Args:
        fasta_file: A fasta file with the sequences to decode.
        batch_generator: Batch generator.
        indices: (A subset of) The sequence indices from the fasta to align (1D).
        batch_size: Controls memory consumption of viterbi.
        model: A learnMSA model which internally might represent multiple pHMM models.
        gap_symbol: Character used to denote missing match positions.
        gap_symbol_insertions: Character used to denote insertions in other sequences.
        build: If "eager", the alignment is decoded when its constructed. If "lazy", the decoding will
                only happen, if the alignment should be printed or written to file.
    """
    def __init__(self, 
                 fasta_file, 
                 batch_generator,
                 indices, 
                 batch_size, 
                 model,
                 gap_symbol="-",
                 gap_symbol_insertions=".",
                 build="eager"):
        self.fasta_file = fasta_file
        self.batch_generator = batch_generator
        self.indices = indices
        self.batch_size = batch_size
        self.model = model
        #encoder model is the same as model but with the MsaHmmLayer removed
        #the output of the encoder model will be the input to viterbi
        #in the default learnMSA, the encoder model is only the Ancestral Probability layer.
        self.encoder_model = None
        for i, layer in enumerate(model.layers[1:]):
            if layer.name.startswith("MsaHmmLayer"):
                encoder_out = model.layers[i].output
                self.msa_hmm_layer = layer
                self.encoder_model = tf.keras.Model(inputs=self.model.inputs, outputs=[encoder_out])
        assert self.encoder_model is not None, "Can not find a MsaHmmLayer in the specified model."
        self.output_alphabet = np.array((fasta.alphabet[:-1] + 
                                        [gap_symbol] + 
                                        [aa.lower() for aa in fasta.alphabet[:-1]] + 
                                        [gap_symbol_insertions, "$"]))
        self.metadata = {}
        if build=="eager":
            self._build_alignment(range(self.msa_hmm_layer.cell.num_models))
        self.num_models = self.msa_hmm_layer.cell.num_models
            
            
    #computes an implicit alignment (without storing gaps)
    #eventually, an alignment with explicit gaps can be written 
    #in a memory friendly manner to file
    def _build_alignment(self, models):
        self.msa_hmm_layer.cell.recurrent_init()
        state_seqs_max_lik = viterbi.get_state_seqs_max_lik(self.fasta_file,
                                                                    self.batch_generator,
                                                                    self.indices,
                                                                    self.batch_size,
                                                                    self.msa_hmm_layer.cell,
                                                                    self.encoder_model)
        lengths = self.msa_hmm_layer.cell.length
        for i in models:
            decoded_data = decode(lengths[i], state_seqs_max_lik[i])
            self.metadata[i] = AlignmentMetaData(*decoded_data)
                              
    
    #use only for low sequence numbers
    def to_string(self, model_index, batch_size=100000, add_block_sep=True):
        alignment_strings_all = []
        n = self.indices.size
        i = 0
        while i < n:
            batch_indices = np.arange(i, min(n, i+batch_size))
            batch_alignment = self.get_batch_alignment(model_index, batch_indices, add_block_sep)
            alignment_strings = self.batch_to_string(batch_alignment)
            alignment_strings_all.extend(alignment_strings)
            i += batch_size
        return alignment_strings_all
    
    
    def to_file(self, filepath, model_index, batch_size=100000, add_block_sep=False):
        with open(filepath, "w") as output_file:
            n = self.indices.size
            i = 0
            while i < n:
                batch_indices = np.arange(i, min(n, i+batch_size))
                batch_alignment = self.get_batch_alignment(model_index, batch_indices, add_block_sep)
                alignment_strings = self.batch_to_string(batch_alignment)
                for s, seq_ind in zip(alignment_strings, batch_indices):
                    seq_id = self.fasta_file.seq_ids[self.indices[seq_ind]]
                    output_file.write(">"+seq_id+"\n")
                    output_file.write(s+"\n")
                i += batch_size
    
    
    #returns a dense matrix representing a subset of sequences
    #as specified by batch_indices with respect to the alignment of all sequences
    #(i.e. the sub alignment can contain gap-only columns and 
    #stacking all batches yields a complete alignment)
    def get_batch_alignment(self, model_index, batch_indices, add_block_sep):
        if not model_index in self.metadata:
            self._build_alignment([model_index])
        b = batch_indices.size
        sequences = np.zeros((b, self.fasta_file.max_len), dtype=np.uint16) + (fasta.s-1)
        for i,j in enumerate(batch_indices):
            l = self.fasta_file.seq_lens[self.indices[j]]
            sequences[i, :l] = self.fasta_file.get_raw_seq(self.indices[j])
        blocks = []  
        if add_block_sep:
            sep = np.zeros((b,1), dtype=np.uint16) + 2*fasta.s
        left_flank_block = get_insertion_block(sequences, 
                                               self.metadata[model_index].left_flank_len[batch_indices],
                                               self.metadata[model_index].left_flank_len_total,
                                               self.metadata[model_index].left_flank_start[batch_indices],
                                               align_to_right=True)
        blocks.append(left_flank_block)
        if add_block_sep:
            blocks.append(sep)
        for i in range(self.metadata[model_index].num_repeats):
            consensus = self.metadata[model_index].consensus[i]
            ins_len = self.metadata[model_index].insertion_lens[i]
            ins_start = self.metadata[model_index].insertion_start[i]
            ins_len_total = self.metadata[model_index].insertion_lens_total[i]
            alignment_block = get_alignment_block(sequences, 
                                                  consensus[batch_indices], 
                                                  ins_len[batch_indices], 
                                                  ins_len_total,
                                                  ins_start[batch_indices])
            blocks.append(alignment_block)
            if add_block_sep:
                blocks.append(sep)
            if i < self.metadata[model_index].num_repeats-1:
                unannotated_segment_l = self.metadata[model_index].unannotated_segments_len[i]
                unannotated_segment_s = self.metadata[model_index].unannotated_segments_start[i]
                unannotated_block = get_insertion_block(sequences, 
                                                        unannotated_segment_l[batch_indices],
                                                        self.metadata[model_index].unannotated_segment_lens_total[i],
                                                        unannotated_segment_s[batch_indices])
                blocks.append(unannotated_block)
                if add_block_sep:
                    blocks.append(sep)
        right_flank_block = get_insertion_block(sequences, 
                                               self.metadata[model_index].right_flank_len[batch_indices],
                                               self.metadata[model_index].right_flank_len_total,
                                               self.metadata[model_index].right_flank_start[batch_indices])
        blocks.append(right_flank_block)
        batch_alignment = np.concatenate(blocks, axis=1)
        return batch_alignment
    
    
    def batch_to_string(self, batch_alignment):
        alignment_arr = self.output_alphabet[batch_alignment]
        alignment_strings = [''.join(s) for s in alignment_arr]
        return alignment_strings
        
    
# utility class used in Alignment
class AlignmentMetaData():
    def __init__(self, 
                 core_blocks, 
                 left_flank, 
                 right_flank, 
                 unannotated_segments):
        self.consensus = np.stack([C for C,_,_,_ in core_blocks])
        self.insertion_lens = np.stack([IL for _,IL,_,_ in core_blocks])
        self.insertion_start = np.stack([IS for _,_,IS,_ in core_blocks])
        self.finished = np.stack([f for _,_,_,f in core_blocks])
        self.left_flank_len = np.stack(left_flank[0])
        self.left_flank_start = np.stack(left_flank[1])
        self.right_flank_len = np.stack(right_flank[0])
        self.right_flank_start = np.stack(right_flank[1])
        if len(unannotated_segments) > 0:
            self.unannotated_segments_len = np.stack([l for l,_ in unannotated_segments])
            self.unannotated_segments_start = np.stack([s for _,s in unannotated_segments])
            self.unannotated_segment_lens_total = np.amax(self.unannotated_segments_len, axis=1)
        else:
            self.unannotated_segment_lens_total = 0
        self.num_repeats = self.consensus.shape[0]
        self.consensus_len = self.consensus.shape[1]
        self.left_flank_len_total = np.amax(self.left_flank_len)
        self.right_flank_len_total = np.amax(self.right_flank_len)
        self.insertion_lens_total = np.amax(self.insertion_lens, axis=1)
        self.alignment_len = (self.left_flank_len_total + 
                              self.consensus_len*self.num_repeats + 
                              np.sum(self.insertion_lens_total) + 
                              np.sum(self.unannotated_segment_lens_total) +
                              self.right_flank_len_total)
  
        
# Given an alignment, computes positions for match expansions and discards depending
# on the following criteria:
# A position is expanded, if an insertion occurs in at least ins_t % of cases or 
# at least k sequences have an insertion of length > ins_long 
# (i.e. filter for very frequent or long insertions)
# In the first case the position is expanded by the average insertion length (counting zeros).
# In the second case the position is expanded by ins_long.
#
# A position is discarded, if it is deleted in at least del_t % of the sequences of all domain blocks and 
# if it has a prior density value in the lower match_prior_threshold % of the range of prior density values.
# (note that as a consequence we keep very conserved positions even if they are deleted frequently in the sequences)
def get_discard_or_expand_positions(alignment, 
                                    model_index, 
                                    #fraction of gaps beyond which a column can be discarded
                                    del_t=0.5, 
                                    #fraction of insertion openings beyond which additional matches are added
                                    ins_t=0.5, 
                                    #percentage of the mid of range prior value below which a match state can be discarded
                                    match_prior_threshold=0.5, 
                                    #insertion length threshold to detect suspiciously long insertions
                                    ins_long=32,
                                    k=2,
                                    verbose=False):
    n = alignment.indices.size
    r = alignment.metadata[model_index].num_repeats
    finished_early = np.sum(alignment.metadata[model_index].finished[:-1], axis=1, keepdims=True)
    num_repeats = r*n-np.sum(finished_early)
    
    #insertions
    #find the fraction of insertion openings in all domain blocks handling repeats as multiple independent hits
    block_ins = np.sum(alignment.metadata[model_index].insertion_lens > 0, axis=(0,1))
    block_ins_frac = block_ins / num_repeats
    left_ins_frac = np.mean(alignment.metadata[model_index].left_flank_len > 0)
    right_ins_frac = np.mean(alignment.metadata[model_index].right_flank_len  > 0)
    ins_frac = np.concatenate([[left_ins_frac], block_ins_frac, [right_ins_frac]], axis=0)
    #compute the average insertion lengths over all sequences/domain blocks 
    #include zeros but do not count "empty" domain hits of finished sequences
    block_ins_lens = np.sum(alignment.metadata[model_index].insertion_lens, axis=(0,1))
    block_ins_avg_lens = block_ins_lens/num_repeats
    left_ins_lens = np.mean(alignment.metadata[model_index].left_flank_len)
    right_ins_lens = np.mean(alignment.metadata[model_index].right_flank_len)
    ins_lens = np.concatenate([[left_ins_lens], block_ins_avg_lens, [right_ins_lens]], axis=0)
    ins_lens = np.ceil(ins_lens).astype(np.int32)
    expand1 = ins_frac > ins_t
    pos_expand1 = np.arange(ins_frac.size, dtype=np.int32)[expand1]
    expansion_lens1 = ins_lens[expand1]
    block_very_long = alignment.metadata[model_index].insertion_lens > ins_long
    block_very_long = np.minimum(np.sum(block_very_long, axis=0), 1) #clip multi domain cases in the same sequence
    block_very_long = np.sum(block_very_long, axis=0)
    left_very_long = np.sum(alignment.metadata[model_index].left_flank_len > ins_long)
    right_very_long = np.sum(alignment.metadata[model_index].right_flank_len  > ins_long)
    very_long = np.concatenate([[left_very_long], block_very_long, [right_very_long]], axis=0)
    expand2 = very_long >= k
    pos_expand2 = np.arange(very_long.size, dtype=np.int32)[expand2]
    expansion_lens2 = np.array([ins_long]*pos_expand2.size, dtype=np.int32)
    #resolve the potential overlap between the two position vectors
    pos_expand, unique_indices = np.unique(np.concatenate([pos_expand2, pos_expand1]), return_index=True) 
    #unique_indices points to the first occurence, therefore we take the entry in pos_expand2
    #over the one in pos_expand1 in case of a dublication
    expansion_lens = np.concatenate([expansion_lens2, expansion_lens1], axis=0)[unique_indices]
    
    #deletions
    #find fraction of gaps in all domain blocks handling repeats as multiple independent hits
    #and without counting "empty" hits of finished sequences
    del_no_finish = np.sum(alignment.metadata[model_index].consensus == -1, axis=1)
    del_no_finish[1:] -= finished_early
    del_frac = np.sum(del_no_finish, axis=0) / num_repeats
    pos_discard1 = np.arange(del_frac.size, dtype=np.int32)[del_frac > del_t]
    #find match states with low prior
    #emissions
    p = []
    for em in alignment.msa_hmm_layer.cell.emitter:
        p_val = em.get_prior_log_density()[model_index]
        if p_val.shape[0] > alignment.msa_hmm_layer.cell.length[model_index]:
            p_val = p_val[1:alignment.msa_hmm_layer.cell.length[model_index]+1]
        p.append(p_val)
    prior_val = np.sum(np.stack(p), axis=0)
    min_prior, max_prior = np.min(prior_val), np.max(prior_val)
    prior_threshold = min_prior + match_prior_threshold * (max_prior - min_prior)
    pos_discard2 = np.arange(prior_val.size, dtype=np.int32)[prior_val <= prior_threshold]
    pos_discard = np.intersect1d(pos_discard1, pos_discard2) 
    
    return pos_expand, expansion_lens, pos_discard


#applies discards and expansions simultaneously to a vector x
#all positions are with respect to the original vector without any modification
#replicates insert_value for the expansions
#assumes that del_marker is a value that does no occur in x
#returns a new vector with all modifications applied
def apply_mods(x, pos_expand, expansion_lens, pos_discard, insert_value, del_marker=-9999):
    #mark discard positions with del_marker, expand thereafter 
    #and eventually remove the marked positions
    x = np.copy(x)
    x[pos_discard] = del_marker
    rep_expand_pos = np.repeat(pos_expand, expansion_lens)
    x = np.insert(x, rep_expand_pos, insert_value, axis=0)
    if len(x.shape) == 2:
        x = x[np.any(x != del_marker, -1)]
    else:
        x = x[x != del_marker]
    return x


# makes updated pos_expand, expansion_lens, pos_discard vectors that fulfill:
#
# - each consecutive segment of discards from i to j is replaced with discards
#   from i+k-1 to j+k and an expansion of length 1 at i+k-1
#   edge cases that do not require an expansion:
#        replaced with discards from i+k to j+k if i+k == 0 and j+k < L-1
#        replaced with discards from i+k-1 to j+k-1 if i+k > 0 and j+k == L-1
#        replaced with discards from i+k to j+k-1 i+k == 0 and j+k == L-1
#
# - an expansion at position i by l is replaced by a discard at i+k-1 and an expansion by l+1 at i+k-1  
#   edge cases that do not require a discard:
#        replaced by an expansion by l at i+k if i+k == 0
#        replaced by an expansion by l at i+k-1 if i+k==L or i+k-1 is already in the discarded positions
#        if all positions are discarded (and the first expansion would add l match states to a model of length 0)
#        the length of the expansion is reduced by 1
#
# k can be any integer 
# L is the length of the array to which the indices of pos_expand and pos_discard belong
def extend_mods(pos_expand, expansion_lens, pos_discard, L, k=0):
    if pos_discard.size == L and pos_expand.size > 0:
        expansion_lens = np.copy(expansion_lens)
        expansion_lens[0] -= 1
    if pos_discard.size > 0:
        #find starting points of all consecutive segments of discards 
        pos_discard_shift = pos_discard + k
        diff = np.diff(pos_discard_shift, prepend=-1)
        diff_where = np.squeeze(np.argwhere(diff > 1))
        segment_starts = np.atleast_1d(pos_discard_shift[diff_where])
        new_pos_discard = np.insert(pos_discard_shift, diff_where, segment_starts-1)
        new_pos_discard = np.unique(new_pos_discard)
        if pos_discard_shift[-1] == L-1:
            new_pos_discard = new_pos_discard[:-1]
            segment_starts = segment_starts[:-1]
        new_pos_expand = segment_starts-1
        new_expansion_lens = np.ones(segment_starts.size, dtype=expansion_lens.dtype)
    else:
        new_pos_discard = pos_discard
        new_pos_expand = np.array([], dtype=pos_expand.dtype)
        new_expansion_lens = np.array([], dtype=expansion_lens.dtype)
    #handle expansions
    if pos_expand.size > 0:
        pos_expand_shift = pos_expand+k
        extend1 = pos_expand_shift > 0
        extend2 = pos_expand_shift < L
        _,indices,_ = np.intersect1d(pos_expand_shift-1, 
                                     np.setdiff1d(np.arange(L), new_pos_discard),
                                     return_indices=True)
        extend3 = np.zeros(pos_expand_shift.size)
        extend3[indices] = 1
        extend = (extend1*extend2*extend3).astype(bool)
        pos_expand_shift[extend1] -= 1
        adj_expansion_lens = np.copy(expansion_lens)
        adj_expansion_lens[extend] += 1
        if new_pos_expand.size == 0:
            new_pos_expand = pos_expand_shift
            new_expansion_lens = adj_expansion_lens
        else:
            if pos_expand_shift.size > 1 and pos_expand_shift[0] == 0 and pos_expand_shift[1] == 0:
                adj_expansion_lens[0] += adj_expansion_lens[1] 
            for i in new_pos_expand:
                a = np.argwhere(pos_expand_shift == i)
                if a.size > 0:
                    adj_expansion_lens[a[0]] += 1
            new_pos_expand = np.concatenate([pos_expand_shift, new_pos_expand])
            new_expansion_lens = np.concatenate([adj_expansion_lens, new_expansion_lens])
            new_pos_expand, indices = np.unique(new_pos_expand, return_index=True)
            new_expansion_lens = new_expansion_lens[indices]
        if new_pos_discard.size > 0:
            new_pos_discard = np.concatenate([new_pos_discard, 
                                              pos_expand_shift[extend]])
            new_pos_discard = np.unique(new_pos_discard)
        else:
            new_pos_discard = pos_expand_shift[extend]
    return new_pos_expand, new_expansion_lens, new_pos_discard


#applies expansions and discards to emission and transition kernels
def update_kernels(alignment,
                   model_index, 
                    pos_expand, 
                    expansion_lens, 
                    pos_discard, 
                    emission_dummy, 
                    transition_dummy,
                    init_flank_dummy):
    L = alignment.msa_hmm_layer.cell.length[model_index]
    emissions = [em.emission_kernel[model_index].numpy() for em in alignment.msa_hmm_layer.cell.emitter]
    transitions = { key : kernel.numpy() 
                         for key, kernel in alignment.msa_hmm_layer.cell.transitioner.transition_kernel[model_index].items()}
    dtype = alignment.msa_hmm_layer.cell.dtype
    emission_dummy = [d((1, em.shape[-1]), dtype).numpy() for d,em in zip(emission_dummy, emissions)]
    transition_dummy = { key : transition_dummy[key](t.shape, dtype).numpy() for key, t in transitions.items()}
    init_flank_dummy = init_flank_dummy((1), dtype).numpy()
    emissions_new = [apply_mods(k, 
                                  pos_expand, 
                                  expansion_lens, 
                                  pos_discard, 
                                  d) for k,d in zip(emissions, emission_dummy)]
    transitions_new = {}
    args1 = extend_mods(pos_expand,expansion_lens,pos_discard,L)
    transitions_new["match_to_match"] = apply_mods(transitions["match_to_match"], 
                                                      *args1,
                                                      transition_dummy["match_to_match"][0])
    transitions_new["match_to_insert"] = apply_mods(transitions["match_to_insert"], 
                                                      *args1,
                                                      transition_dummy["match_to_insert"][0])
    transitions_new["insert_to_match"] = apply_mods(transitions["insert_to_match"], 
                                                      *args1,
                                                      transition_dummy["insert_to_match"][0])
    transitions_new["insert_to_insert"] = apply_mods(transitions["insert_to_insert"], 
                                                      *args1,
                                                      transition_dummy["insert_to_insert"][0])
    args2 = extend_mods(pos_expand,expansion_lens,pos_discard,L+1,k=1)
    transitions_new["match_to_delete"] = apply_mods(transitions["match_to_delete"],
                                                     *args2,
                                                      transition_dummy["match_to_delete"][0])
    args3 = extend_mods(pos_expand,expansion_lens,pos_discard,L+1)
    transitions_new["delete_to_match"] = apply_mods(transitions["delete_to_match"],
                                                     *args3,
                                                      transition_dummy["delete_to_match"][0])
    transitions_new["delete_to_delete"] = apply_mods(transitions["delete_to_delete"],
                                                     *args1,
                                                      transition_dummy["delete_to_delete"][0])
    
    #always reset the multi-hit transitions:
    transitions_new["left_flank_loop"] = transition_dummy["left_flank_loop"] 
    transitions_new["left_flank_exit"] = transition_dummy["left_flank_exit"] 
    init_flank_new = init_flank_dummy
    transitions_new["right_flank_loop"] = transition_dummy["right_flank_loop"] 
    transitions_new["right_flank_exit"] = transition_dummy["right_flank_exit"] 
    transitions_new["end_to_unannotated_segment"] = transition_dummy["end_to_unannotated_segment"] 
    transitions_new["end_to_right_flank"] = transition_dummy["end_to_right_flank"] 
    transitions_new["end_to_terminal"] = transition_dummy["end_to_terminal"] 
    transitions_new["unannotated_segment_loop"] = transition_dummy["unannotated_segment_loop"] 
    transitions_new["unannotated_segment_exit"] = transition_dummy["unannotated_segment_exit"] 
    
    # Maybe TODO?: Discarding or extending positions has the side effect of changing all probabilities
    # in begin-state transition distribution. E.g. 
    # Depending on discarded positions, adjust weights such that the residual distribution after 
    # discarding some match states is unaffected.
    # If an insert position is expanded, the transitions from begin to the new match states should have 
    # probabilities according to the initial dummy distribution and the weights of the old transitions 
    # should also be corrected accordingly.
    
    transitions_new["begin_to_match"] = apply_mods(transitions["begin_to_match"], 
                                                      pos_expand, 
                                                      expansion_lens, 
                                                      pos_discard, 
                                                      transition_dummy["begin_to_match"][1])
    if 0 in pos_expand:
        transitions_new["begin_to_match"][0] = transition_dummy["begin_to_match"][0]
        
    if L in pos_expand:
        transitions["match_to_end"][-1] = transition_dummy["match_to_end"][0]
    transitions_new["match_to_end"] = apply_mods(transitions["match_to_end"], 
                                                  pos_expand, 
                                                  expansion_lens, 
                                                  pos_discard, 
                                                  transition_dummy["match_to_end"][0])
    return transitions_new, emissions_new, init_flank_new


    

def compute_loglik(alignment, max_ll_estimate = 200000):
    if alignment.fasta_file.num_seq > max_ll_estimate:
        #estimate the ll only on a subset, otherwise for millions of 
        # sequences this step takes rather long for little benefit
        ll_subset = np.arange(alignment.fasta_file.num_seq)
        np.random.shuffle(ll_subset)
        ll_subset = ll_subset[:max_ll_estimate]
        ll_subset = np.sort(ll_subset)
    else:
        #use the sorted indices for optimal length distributions in batches
        ll_subset = alignment.fasta_file.sorted_indices
    ds = train.make_dataset(ll_subset, 
                            alignment.batch_generator,
                            alignment.batch_size, 
                            shuffle=False)
    loglik = np.zeros((alignment.msa_hmm_layer.cell.num_models))
    for x, _ in ds:
        loglik += np.sum(alignment.model(x), axis=1)
    loglik /= ll_subset.size
    prior = alignment.msa_hmm_layer.cell.get_prior_log_density().numpy()/alignment.fasta_file.num_seq
    return loglik, prior

    
def get_full_length_estimate(fasta_file, config):
    n = fasta_file.num_seq
    #ignore short sequences for all surgery iterations except the last
    k = int(min(n*config["surgery_quantile"], 
                max(0, n-config["min_surgery_seqs"])))
    #a rough estimate of a set of only full-length sequences
    full_length_estimate = fasta_file.sorted_indices[k:]
    return full_length_estimate


def get_initial_model_lengths(fasta_file, config):
    #initial model length
    model_length = np.quantile(fasta_file.seq_lens, q=config["length_init_quantile"])
    model_length *= config["len_mul"]
    model_length = max(3, int(np.floor(model_length)))
    return [model_length] * config["num_models"]
    
    
def _make_defaults_if_none(fasta_file, config, model_generator, batch_generator):
    if model_generator is None:
        model_generator = train.default_model_generator
    if batch_generator is None:
        batch_generator = train.DefaultBatchGenerator(fasta_file, config["num_models"])
    return model_generator, batch_generator


def _fasta_file_messages(fasta_file, seq_count_warning_threshold=100):
    if fasta_file.gaps:
        print(f"Warning: The file {fasta_file.filename} already contains gaps. Realining the raw sequences.")
    if fasta_file.num_seq < seq_count_warning_threshold:
        print(f"Warning: You are aligning {fasta_file.num_seq} sequences, although learnMSA is designed for large scale alignments. We recommend to have a sufficiently deep training dataset of at least {seq_count_warning_threshold} sequences for accurate results.")
        
        
def _get_initializers(alignment):
    emission_init = [em.emission_init 
                       for em in alignment.msa_hmm_layer.cell.emitter]
    transition_init = alignment.msa_hmm_layer.cell.transitioner.transition_init
    flank_init = alignment.msa_hmm_layer.cell.transitioner.flank_init
    return emission_init, transition_init, flank_init