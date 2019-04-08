import torch
import torch.nn as nn
import torch.nn.functional as F
from .beam_search import BeamSearch
import numpy as np


class EncoderDecoderModel(nn.Module):
    def __init__(self, encoder, decoder):
        super(EncoderDecoderModel, self).__init__()
        self._encoder = encoder
        self._decoder = decoder

    @property
    def encoder(self):
        return self._encoder
    
    @property
    def decoder(self):
        return self._decoder

    def forward(self, inputs):

        assert torch.all(
            inputs["source_lengths"][:-1] >= inputs["source_lengths"][1:])

        encoder_context, encoder_state = self._encoder(
            inputs["source_features"], 
            inputs["source_lengths"]) 

        if inputs.get("multi_ref", False):
            encoder_context = encoder_context[inputs["target_source_ids"]]
            encoder_state = encoder_state[:,inputs["target_source_ids"]] 
            mask = inputs.get("source_mask", None)
            if mask is not None:
                mask = mask[inputs["target_source_ids"]]
        else:
            mask = inputs.get("source_mask", None)

        # TODO change decoder interface to put everything in a dict.
        logits, attn, decoder_state = self._decoder(
            inputs["target_input_features"], 
            encoder_context, 
            encoder_state, 
            context_mask=mask)

        state = {
            "context_attention": attn["attention"],
            "decoder_state": decoder_state,
            "encoder_state": encoder_state,
            "encoder_context": encoder_context
        }

        return logits, state

    def xentropy(self, batch, reduction="mean", return_state=False):

        logits, state = self.forward(batch)
        pad_index = self.decoder.embedding_context.vocab.pad_index

        elem_xent = F.cross_entropy(
            logits.permute(0, 2, 1), 
            batch["target_output_features"]["tokens"].t(),
            ignore_index=pad_index,
            reduction="none")
        
        if reduction == "mean":
            num_tokens = batch["target_lengths"].sum().float()
            mean_xent = elem_xent.sum() / num_tokens
            result = mean_xent
        elif reduction == "sum":
            result = elem_xent.sum()
        elif reduction == "none":
            result = elem_xent
        else:
            raise Exception("reduction must be 'mean', 'sum', or 'none'.")

        if return_state:
            return result, state
        else:
            return result

    def predict(self, inputs):
        context, state = self._encoder(
            inputs["source_features"], 
            inputs["source_lengths"]) 
        return self._decoder.predict(context, state)

    def predict_tokens(self, inputs):
        predicted_indices = self.predict(inputs)
        return self.decoder.embedding_context.convert_index_tensor(
            predicted_indices)

    def _sort_inputs(self, inputs):
        src_lengths, sorted_order = torch.sort(
            inputs["source_lengths"], descending=True)
        src_features = {feat: values[sorted_order] 
                        for feat, values in inputs["source_features"].items()}
        
        inv_order = torch.sort(sorted_order)[1]
        new_inputs = {"source_lengths": src_lengths, 
                      "source_features": src_features}
        return new_inputs, inv_order

    def greedy_decode(self, inputs, sorted=False, return_tokens=True, 
                      max_steps=100, copy_unknown=False):
        if not sorted:
            orig_inputs = inputs
            inputs, inv_order = self._sort_inputs(inputs)
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 
            output, decoder_states = self._decoder.decode(
                context, state, max_steps=max_steps)
            output = output[inv_order]
            for decoder_state in decoder_states:
                if "attention" in decoder_state:
                    decoder_state["attention"] = (
                        decoder_state["attention"][:,inv_order])
            inputs = orig_inputs
        else:
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 
            output, decoder_states = self._decoder.decode(
                context, state, max_steps=max_steps)

        # TODO remove this and make a separate model_wrapper.
        if return_tokens:
             
            tokens = self.decoder.embedding_context.convert_index_tensor(
                output)
            if copy_unknown and "attention" in decoder_states[0]:
                unknown_tokens = output.eq(
                    self.decoder.embedding_context.vocab.unknown_index)
                src_vcb = self.encoder.embedding_context.named_vocabs["tokens"]
                batches, steps = np.where(unknown_tokens.data.numpy())
                for batch, step in zip(*np.where(unknown_tokens.data.numpy())):
                    attention = decoder_states[step]["attention"][0, batch]
                    src = torch.argmax(attention).item()
                    tkn_idx = inputs["source_features"]["tokens"][batch, src]
                    tkn = src_vcb[tkn_idx]
                    tokens[batch][step] = tkn

            return tokens
        else:
            return output

    def decode(self, inputs, sorted=False, return_tokens=True, max_steps=100):
        if not sorted:
            inputs, inv_order = self._sort_inputs(inputs)
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 
            output = self._decoder.decode(context, state, max_steps=max_steps)
            output = output[inv_order]
        else:
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 
            output = self._decoder.decode(context, state, max_steps=max_steps)

        if return_tokens:
            output = self.decoder.embedding_context.convert_index_tensor(
                output)

        return output

    def beam_decode(self, inputs, beam_size=8, sorted=False, 
                    return_tokens=True, return_scores=False, max_steps=300,
                    rescoring_func=None):

        if not sorted:
            inputs, inv_order = self._sort_inputs(inputs)
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 
            beam = BeamSearch(self.decoder, state, context, 
                              beam_size=beam_size, max_steps=max_steps,
                              rescoring_func=rescoring_func)
            beam.search()
            beam.sort_by_score()
            output = beam.candidates[inv_order]
        else:
            context, state = self._encoder(
                inputs["source_features"], 
                inputs["source_lengths"]) 

            beam = BeamSearch(self.decoder, state, context, 
                              beam_size=beam_size, max_steps=max_steps,
                              rescoring_func=rescoring_func)
            beam.search()
            beam.sort_by_score()
            output = beam.candidates

        if return_tokens:
            output = self.decoder.embedding_context.convert_index_tensor(
                output)
        if return_scores:
            return output, beam.scores
        else:
            return output

    def initialize_parameters(self):
        print(" Initializing encoder parameters.")
        self.encoder.initialize_parameters()
        print(" Initializing decoder parameters.")
        self.decoder.initialize_parameters()