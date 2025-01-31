# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Code for constructing the model."""
from typing import Any, Mapping, Optional, Union

from absl import logging
from alphafold.common import confidence
from alphafold.model import features
from alphafold.model import modules
from alphafold.model import modules_multimer
from alphafold.common import residue_constants
import haiku as hk
import jax
import ml_collections
import numpy as np
import tensorflow.compat.v1 as tf
import tree

def get_confidence_metrics(
    prediction_result: Mapping[str, Any],
    mask: Any,
    rank_by: str = "auto") -> Mapping[str, Any]:
  """Post processes prediction_result to get confidence metrics."""  
  confidence_metrics = {}

  plddt = confidence.compute_plddt(prediction_result['predicted_lddt']['logits'])
  confidence_metrics['plddt'] = plddt  
  confidence_metrics["mean_plddt"] = (plddt * mask).sum()/mask.sum()

  if 'predicted_aligned_error' in prediction_result:
    confidence_metrics.update(confidence.compute_predicted_aligned_error(
        logits=prediction_result['predicted_aligned_error']['logits'],
        breaks=prediction_result['predicted_aligned_error']['breaks']))
    
    confidence_metrics['ptm'] = confidence.predicted_tm_score(
        logits=prediction_result['predicted_aligned_error']['logits'],
        breaks=prediction_result['predicted_aligned_error']['breaks'],
        residue_weights=mask)    

    asym_id = prediction_result["predicted_aligned_error"].get("asym_id",None)
    if asym_id is not None and np.unique(asym_id).shape[0] > 1:
      # Compute the ipTM only for the multimer model.
      confidence_metrics['iptm'] = confidence.predicted_tm_score(
          logits=prediction_result['predicted_aligned_error']['logits'],
          breaks=prediction_result['predicted_aligned_error']['breaks'],
          residue_weights=mask, asym_id=asym_id)

    # decide what metric to use for the mean_score
    if rank_by == "auto":
      if  "iptm" in confidence_metrics:
        rank_by = "multimer"
      elif "ptm" in confidence_metrics:
        rank_by = "ptm"
      else:
        rank_by = "plddt"
    else:
      if rank_by in ["multimer","iptm"] and "iptm" not in confidence_metrics: rank_by = "ptm"
      if rank_by == "ptm" and "ptm" not in confidence_metrics: rank_by = "plddt"

    # compute mean_score
    if rank_by == "multimer": mean_score = 80 * confidence_metrics["iptm"] + 20 * confidence_metrics["ptm"]
    if rank_by == "iptm":     mean_score = 100 * confidence_metrics["iptm"]
    if rank_by == "ptm":      mean_score = 100 * confidence_metrics["ptm"]
    if rank_by == "plddt":    mean_score = confidence_metrics["mean_plddt"]
    confidence_metrics["ranking_confidence"] = mean_score
  
  return confidence_metrics


class RunModel:
  """Container for JAX model."""

  def __init__(self,
               config: ml_collections.ConfigDict,
               params: Optional[Mapping[str, Mapping[str, np.ndarray]]] = None,
               is_training = False):
    
    self.config = config
    self.params = params
    self.multimer_mode = config.model.global_config.multimer_mode


    if self.multimer_mode:
      def _forward_fn(batch):
        model = modules_multimer.AlphaFold(self.config.model)
        return model(batch, is_training=is_training)
    else:
      def _forward_fn(batch):
        if self.config.data.eval.num_ensemble == 1:
          model = modules.AlphaFold_noE(self.config.model)
          return model(batch, is_training=is_training)
        else:
          model = modules.AlphaFold(self.config.model)
          return model(
              batch,
              is_training=is_training,
              compute_loss=False,
              ensemble_representations=True)

    self.apply = jax.jit(hk.transform(_forward_fn).apply)
    self.init = jax.jit(hk.transform(_forward_fn).init)

  def init_params(self, feat: features.FeatureDict, random_seed: int = 0):
    """Initializes the model parameters.

    If none were provided when this class was instantiated then the parameters
    are randomly initialized.

    Args:
      feat: A dictionary of NumPy feature arrays as output by
        RunModel.process_features.
      random_seed: A random seed to use to initialize the parameters if none
        were set when this class was initialized.
    """
    if not self.params:
      # Init params randomly.
      rng = jax.random.PRNGKey(random_seed)
      self.params = hk.data_structures.to_mutable_dict(
          self.init(rng, feat))
      logging.warning('Initialized parameters randomly')

  def process_features(
      self,
      raw_features: Union[tf.train.Example, features.FeatureDict],
      random_seed: int) -> features.FeatureDict:
    """Processes features to prepare for feeding them into the model.

    Args:
      raw_features: The output of the data pipeline either as a dict of NumPy
        arrays or as a tf.train.Example.
      random_seed: The random seed to use when processing the features.

    Returns:
      A dict of NumPy feature arrays suitable for feeding into the model.
    """

    if self.multimer_mode:
      return raw_features

    # Single-chain mode.
    if isinstance(raw_features, dict):
      return features.np_example_to_features(
          np_example=raw_features,
          config=self.config,
          random_seed=random_seed)
    else:
      return features.tf_example_to_features(
          tf_example=raw_features,
          config=self.config,
          random_seed=random_seed)

  def eval_shape(self, feat: features.FeatureDict) -> jax.ShapeDtypeStruct:
    self.init_params(feat)
    logging.debug('Running eval_shape with shape(feat) = %s',
                 tree.map_structure(lambda x: x.shape, feat))
    shape = jax.eval_shape(self.apply, self.params, jax.random.PRNGKey(0), feat)
    logging.info('Output shape was %s', shape)
    return shape

  def predict(self,
              feat: features.FeatureDict,
              random_seed: int = 0,
              verbose: bool = False,
              prediction_callback: Any = None) -> Mapping[str, Any]:
    """Makes a prediction by inferencing the model on the provided features.

    Args:
      feat: A dictionary of NumPy feature arrays as output by
        RunModel.process_features.
      random_seed: The random seed to use when running the model. In the
        multimer model this controls the MSA sampling.

    Returns:
      A dictionary of model outputs.
    """
    self.init_params(feat)
    logging.info('Running predict with shape(feat) = %s',
                 tree.map_structure(lambda x: x.shape, feat))
    
    aatype = feat["aatype"]
    if self.multimer_mode:
      num_iters = self.config.model.num_recycle + 1
      L = aatype.shape[0]
    else:
      num_iters = self.config.model.num_recycle + 1
      num_ensemble = self.config.data.eval.num_ensemble
      L = aatype.shape[1]
    
    result = {"prev":{'prev_msa_first_row': np.zeros([L,256]),
                      'prev_pair': np.zeros([L,L,128]),
                      'prev_pos': np.zeros([L,37,3])}}
        
    r = 0
    key = jax.random.PRNGKey(random_seed)
    stop = False
    while r < num_iters:
        if self.multimer_mode:
            sub_feat = feat
        else:
            s = r * num_ensemble
            e = (r+1) * num_ensemble
            sub_feat = jax.tree_map(lambda x:x[s:e], feat)
            
        sub_feat["prev"] = result["prev"]
        key, sub_key = jax.random.split(key)
        result = self.apply(self.params, sub_key, sub_feat)
        seq_mask = feat["seq_mask"] if self.multimer_mode else feat["seq_mask"][0]
        confidences = get_confidence_metrics(result, mask=seq_mask, rank_by=self.config.model.rank_by)

        if confidences["ranking_confidence"] > self.config.model.stop_at_score:
            stop = True

        if self.config.model.recycle_early_stop_tolerance > 0:
          ca_idx = residue_constants.atom_order['CA']
          if r > 0:
            # Early stopping criteria
            pos = result["prev"]["prev_pos"][:,ca_idx]
            dist = lambda x: np.sqrt(np.square(x[:,None]-x[None,:]).sum(-1))
            sq_diff = np.square(dist(pos) - dist(prev_pos))
            mask_2d = seq_mask[:,None] * seq_mask[None,:]
            confidences["diff"] = np.sqrt((sq_diff * mask_2d).sum()/mask_2d.sum())
            if confidences["diff"] < self.config.model.recycle_early_stop_tolerance:
              stop = True
          prev_pos = result["prev"]["prev_pos"][:,ca_idx]
        
        result.update(confidences)
        if prediction_callback is not None: prediction_callback(result, r)

        if verbose:
          print_line = f"recycle={r} plddt={confidences['mean_plddt']:.3g}"
          for k in ["ptm","iptm","diff"]:
            if k in confidences: print_line += f" {k}:{confidences[k]:.3g}"
          print(print_line)
        r += 1
        if stop: break

    logging.info('Output shape was %s', tree.map_structure(lambda x: x.shape, result))
    return result, (r-1)