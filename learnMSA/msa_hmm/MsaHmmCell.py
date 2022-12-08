import os
import tensorflow as tf
import numpy as np
import learnMSA.msa_hmm.Emitter as emit
import learnMSA.msa_hmm.Transitioner as trans


class MsaHmmCell(tf.keras.layers.Layer):
    """ A general cell for (p)HMM training. It is meant to be used with the generic RNN-layer.
        It computes the likelihood of a batch of sequences, computes a prior value and provides 
        functionality (through the injected emitter and transitioner) to construct the emission- 
        and transition-matricies also used elsewhere e.g. during Viterbi.
        Based on https://github.com/mslehre/classify-seqs/blob/main/HMMCell.py.
    Args:
        length: Model length / number of match states or a list of lengths.
        emitter: An object or a list of objects following the emitter interface (see MultinomialAminoAcidEmitter).
        transitioner: An object following the transitioner interface (see ProfileHMMTransitioner).
        dtype: The datatype of the cell.
    """
    def __init__(self,
                 length, 
                 emitter = emit.ProfileHMMEmitter(),
                 transitioner = trans.ProfileHMMTransitioner(),
                 dtype=tf.float32,
                 **kwargs
                ):
        super(MsaHmmCell, self).__init__(name="MsaHmmCell", dtype=dtype, **kwargs)
        self.length = [length] if not hasattr(length, '__iter__') else length 
        self.num_models = len(self.length)
        self.emitter = [emitter] if not hasattr(emitter, '__iter__') else emitter 
        self.transitioner = transitioner
        #number of emitting states, i.e. not counting flanking states and deletions
        self.num_states = [2 * length + 3 for length in self.length]  
        self.num_states_implicit = [num_states + length + 2 
                                    for num_states, length in zip(self.num_states, self.length)]
        self.max_num_states = max(self.num_states)
        self.state_size = (tf.TensorShape([None, self.max_num_states]), tf.TensorShape([None]))
        self.output_size = tf.TensorShape([None, self.max_num_states])
        for em in self.emitter:
            em.cell_init(self)
        self.transitioner.cell_init(self)
            
    def build(self, input_shape):
        self.dim = input_shape[-1]
        for em in self.emitter:
            em.build(input_shape)
        self.transitioner.build(input_shape)

    def recurrent_init(self):
        self.transitioner.recurrent_init()
        for em in self.emitter:
            em.recurrent_init()
        self.init = True
    
    def make_initial_distribution(self):
        """Constructs the initial state distribution which depends on the transition probabilities.
            See ProfileHMMTransitioner.
        Returns:
            A probability distribution of shape: (q)
        """
        return self.transitioner.make_initial_distribution()
    
    def emission_probs(self, inputs):
        """ Computes the probabilities of emission per state for the given observation. Multiple emitters
            are multiplied.
        Args:
            inputs: A batch of sequence positions.
        """
        em_probs = self.emitter[0](inputs)
        for em in self.emitter[1:]:
            em_probs *= em(inputs)
        return em_probs

    def call(self, inputs, states, training=None):
        """ Computes one recurrent step of the Forward DP.
        """
        old_forward, old_loglik = states
        inputs = tf.reshape(inputs, (self.num_models, -1, self.dim))
        E = self.emission_probs(inputs)
        if self.init:
            forward = tf.multiply(E, old_forward, name="forward")
            self.init = False
        else:
            R = self.transitioner(old_forward)
            forward = tf.multiply(E, R, name="forward")
        S = tf.reduce_sum(forward, axis=-1, keepdims=True, name="loglik")
        loglik = old_loglik + tf.math.log(S) 
        forward = forward / S 
        new_state = [forward, loglik]
        return forward, new_state

    def get_initial_state(self, inputs=None, batch_size=None, _dtype=None):
        init_dist = tf.repeat(self.make_initial_distribution(), repeats=batch_size, axis=0)
        init_dist = tf.transpose(init_dist, (1,0,2))
        loglik = tf.zeros((self.num_models, batch_size, 1), dtype=self.dtype)
        S = [init_dist, loglik]
        return S

    def get_prior_log_density(self, add_metrics=False):  
        em_priors = [tf.reduce_sum(em.get_prior_log_density(), 1) for em in self.emitter]
        trans_priors = self.transitioner.get_prior_log_densities()
        prior = sum(em_priors) + sum(trans_priors.values())
        if add_metrics:
            for i,d in enumerate(em_priors):
                d = tf.reduce_mean(d)
                self.add_metric(d, "mean_model_em_prior_"+str(i))
            for name, d in trans_priors.items():
                d = tf.reduce_mean(d)
                self.add_metric(d, "mean_model_"+name)
        return prior

