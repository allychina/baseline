import tensorflow as tf
import json
from google.protobuf import text_format
from tensorflow.python.platform import gfile
from baseline.tf.tfy import *
from baseline.w2v import RandomInitVecModel

class Seq2SeqBaseTf:

    def save(self, model_base):
        pass

    def __init__(self):
        pass

    def step(self, src, src_len, dst, dst_len):
        """
        Generate probability distribution over output V for next token
        """
        feed_dict = {self.src: src, self.tgt: dst, self.pkeep: 1.0}
        return self.sess.run(self.probs, feed_dict=feed_dict)


    def make_cell(self, hsz, nlayers, rnntype):
        
        if nlayers > 1:
            cell = tf.contrib.rnn.MultiRNNCell([new_rnn_cell(hsz, rnntype, True) for _ in range(nlayers)], state_is_tuple=True)
            return cell
        return new_rnn_cell(hsz, rnntype, False)

    def create_loss(self):
        
        targets = tf.unstack(tf.transpose(self.tgt[:,1:], perm=[1, 0]))
        predictions = tf.unstack(self.preds)
        bests = tf.unstack(self.best)

        with tf.name_scope("Loss"):

            log_perp_list = []
            total_list = []
            # For each t in T
            for preds_i, best_i, target_i in zip(predictions, bests, targets):
                # Mask against (B)
                mask = tf.cast(tf.sign(target_i), tf.float32)
                # self.preds_i = (B, V)
                #best_i = tf.cast(tf.argmax(preds_i, 1), tf.int32)
                err = tf.cast(tf.not_equal(tf.cast(best_i, tf.int32), target_i), tf.float32)
                # Gives back (B, V)
                xe = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=preds_i, labels=target_i)

                log_perp_list.append(xe * mask)
                total_list.append(tf.reduce_sum(mask))
                
            log_perps = tf.add_n(log_perp_list)
            totalsz = tf.add_n(total_list)
            log_perps /= totalsz

            cost = tf.reduce_sum(log_perps)

            batchsz = tf.cast(tf.shape(targets[0])[0], tf.float32)
            avg_cost = cost/batchsz
            return avg_cost


class Seq2SeqModelTf_v1_1(Seq2SeqBaseTf):

    def create_loss(self):

        targets = tf.transpose(self.tgt[:,1:], perm=[1, 0])
        targets = targets[0:self.mx_tgt_len,:]
        target_lens = self.tgt_len - 1
        with tf.name_scope("Loss"):
            losses = tf.nn.sparse_softmax_cross_entropy_with_logits(
                logits=self.preds, labels=targets)

            loss_mask = tf.sequence_mask(
                tf.to_int32(target_lens), tf.to_int32(tf.shape(targets)[0]))
            losses = losses * tf.transpose(tf.to_float(loss_mask), [1, 0])
    
            losses = tf.reduce_sum(losses)
            losses /= tf.cast(tf.reduce_sum(target_lens), tf.float32)
            return losses

    def __init__(self):
        pass

    def params(self, sess, embed1, embed2, mxlen, hsz, nlayers=1, attn=False, rnntype='lstm', predict=False):
        self.sess = sess
        # These are going to be (B,T)
        self.src = tf.placeholder(tf.int32, [None, mxlen], name="src")
        self.tgt = tf.placeholder(tf.int32, [None, mxlen], name="tgt")
        self.pkeep = tf.placeholder(tf.float32, name="pkeep")

        self.src_len = tf.placeholder(tf.int32, [None], name="src_len")
        self.tgt_len = tf.placeholder(tf.int32, [None], name="tgt_len")
        self.mx_tgt_len = tf.placeholder(tf.int32, name="mx_tgt_len")

        self.vocab1 = embed1.vocab
        self.vocab2 = embed2.vocab

        self.mxlen = mxlen
        self.hsz = hsz
        self.nlayers = nlayers
        self.rnntype = rnntype
        self.attn = attn

        GO = self.vocab2['<GO>']
        EOS = self.vocab2['<EOS>']
        vsz = embed2.vsz + 1

        assert embed1.dsz == embed2.dsz
        self.dsz = embed1.dsz

        with tf.name_scope("LUT"):
            Wi = tf.Variable(tf.constant(embed1.weights, dtype=tf.float32), name="Wi")
            Wo = tf.Variable(tf.constant(embed2.weights, dtype=tf.float32), name="Wo")

            embed_in = tf.nn.embedding_lookup(Wi, self.src)
            
        with tf.name_scope("Recurrence"):
            rnn_enc_tensor, final_encoder_state = self.encode(embed_in, self.src)
            #print(final_encoder_state[0], final_encoder_state[1])
            batch_sz = tf.shape(rnn_enc_tensor)[0]

            with tf.variable_scope("dec") as vs:
                proj = dense_layer(vsz)
                rnn_dec_cell = self._attn_cell(rnn_enc_tensor) #[:,:-1,:])

                if self.attn is True:
                    initial_state = rnn_dec_cell.zero_state(dtype=tf.float32, batch_size=batch_sz)
                else:
                    initial_state = final_encoder_state

                if predict is True:
                    helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(Wo, tf.fill([batch_sz], GO), EOS)
                else:
                    helper = tf.contrib.seq2seq.TrainingHelper(inputs=tf.nn.embedding_lookup(Wo, self.tgt), sequence_length=self.tgt_len)
                decoder = tf.contrib.seq2seq.BasicDecoder(cell=rnn_dec_cell, helper=helper, initial_state=initial_state, output_layer=proj)
                final_outputs, final_decoder_state, _ = tf.contrib.seq2seq.dynamic_decode(decoder, impute_finished=True, output_time_major=True, maximum_iterations=self.mxlen)
                self.preds = final_outputs.rnn_output
                best = final_outputs.sample_id

        with tf.name_scope("Output"):
            self.best = tf.identity(best, name='best')
            self.probs = tf.map_fn(lambda x: tf.nn.softmax(x, name='probs'), self.preds)
        return self

    def _attn_cell(self, rnn_enc_tensor):
        cell = new_multi_rnn_cell(self.hsz, self.rnntype, self.nlayers)
        if self.attn:
            attn_mech = tf.contrib.seq2seq.LuongAttention(self.hsz, rnn_enc_tensor, self.src_len) 
            cell = tf.contrib.seq2seq.AttentionWrapper(cell, attn_mech, self.hsz, name='dyn_attn_cell')
        return cell

    def encode(self, embed_in, src):
        with tf.name_scope('encode'):
            # List to tensor, reform as (T, B, W)
            embed_in_seq = tensor2seq(embed_in)
            rnn_enc_cell = new_multi_rnn_cell(self.hsz, self.rnntype, self.nlayers)
            #TODO: Switch to tf.nn.rnn.dynamic_rnn()
            rnn_enc_seq, final_encoder_state = tf.contrib.rnn.static_rnn(rnn_enc_cell, embed_in_seq, scope='rnn_enc', dtype=tf.float32)
            # This comes out as a sequence T of (B, D)
            return seq2tensor(rnn_enc_seq), final_encoder_state

    def save_md(self, model_base):

        path_and_file = model_base.split('/')
        outdir = '/'.join(path_and_file[:-1])
        base = path_and_file[-1]
        tf.train.write_graph(self.sess.graph_def, outdir, base + '.graph', as_text=False)

        state = {"attn": self.attn, "hsz": self.hsz, "dsz": self.dsz, "rnntype": self.rnntype, "nlayers": self.nlayers, "mxlen": self.mxlen }
        with open(model_base + '.state', 'w') as f:
            json.dump(state, f)

        with open(model_base + '-1.vocab', 'w') as f:
            json.dump(self.vocab1, f)      

        with open(model_base + '-2.vocab', 'w') as f:
            json.dump(self.vocab2, f)     
        

    def save(self, model_base):
        self.save_md(model_base)
        self.saver.save(self.sess, model_base + '.model')

    def restore_md(self, model_base):

        with open(model_base + '-1.vocab', 'r') as f:
            self.vocab1 = json.load(f)

        with open(model_base + '-2.vocab', 'r') as f:
            self.vocab2 = json.load(f)

        with open(model_base + '.state', 'r') as f:
            state = json.load(f)
            self.attn = state['attn']
            self.hsz = state['hsz']
            self.dsz = state['dsz']
            self.rnntype = state['rnntype']
            self.nlayers = state['nlayers']
            self.mxlen = state['mxlen']

    def restore_graph(self, base):
        with open(base + '.graph', 'rb') as gf:
            gd = tf.GraphDef()
            gd.ParseFromString(gf.read())
            self.sess.graph.as_default()
            tf.import_graph_def(gd, name='')

    def step(self, src, src_len, dst, dst_len):
        """
        Generate probability distribution over output V for next token
        """
        feed_dict = {self.src: src, self.src_len: src_len, self.tgt: dst, self.tgt_len: dst_len, self.pkeep: 1.0}
        return self.sess.run(self.probs, feed_dict=feed_dict)


def create_model(embedding1, embedding2, **kwargs):
    hsz = int(kwargs['hsz'])
    attn = bool(kwargs.get('attn', False))
    layers = int(kwargs.get('layers', 1))
    rnntype = kwargs.get('rnntype', 'lstm')
    mxlen = kwargs.get('mxlen', 100)
    predict = kwargs.get('predict', False)
    sess = kwargs.get('sess', tf.Session())
    enc_dec = Seq2SeqModelTf_v1_1()
    enc_dec.params(sess, embedding1, embedding2, mxlen, hsz, layers, attn, rnntype, predict)
    return enc_dec
