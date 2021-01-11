# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contrastive loss functions."""

# Import libraries
import tensorflow as tf

LARGE_NUM = 1e9


def cross_replica_concat(tensor: tf.Tensor, num_replicas: int) -> tf.Tensor:
  """Reduce a concatenation of the `tensor` across multiple replicas.

  Args:
    tensor: `tf.Tensor` to concatenate.
    num_replicas: `int` number of replicas.

  Returns:
    Tensor of the same rank as `tensor` with first dimension `num_replicas`
    times larger.
  """
  if num_replicas <= 1:
    return tensor

  replica_context = tf.distribute.get_replica_context()
  with tf.name_scope('cross_replica_concat'):
    # This creates a tensor that is like the input tensor but has an added
    # replica dimension as the outermost dimension. On each replica it will
    # contain the local values and zeros for all other values that need to be
    # fetched from other replicas.
    ext_tensor = tf.scatter_nd(
        indices=[[replica_context.replica_id_in_sync_group]],
        updates=[tensor],
        shape=tf.concat([[num_replicas], tf.shape(tensor)], axis=0))

    # As every value is only present on one replica and 0 in all others, adding
    # them all together will result in the full tensor on all replicas.
    ext_tensor = replica_context.all_reduce(tf.distribute.ReduceOp.SUM,
                                            ext_tensor)

    # Flatten the replica dimension.
    # The first dimension size will be: tensor.shape[0] * num_replicas
    # Using [-1] trick to support also scalar input.
    return tf.reshape(ext_tensor, [-1] + ext_tensor.shape.as_list()[2:])


class ContrastiveLoss(object):
  """Contrastive training loss function."""

  def __init__(self, projection_norm: bool = True, temperature: float = 1.0):
    """Initializes `ContrastiveLoss`.

    Args:
      projection_norm: whether or not to use normalization on the hidden vector.
      temperature: a `floating` number for temperature scaling.
    """
    self._projection_norm = projection_norm
    self._temperature = temperature

  def __call__(self, projection1: tf.Tensor, projection2: tf.Tensor):
    """Compute the contrastive loss for contrastive learning.

    Note that projection2 is generated with the same batch (same order) of raw
    images, but with different augmentation. More specifically:
    image[i] -> random augmentation 1 -> projection -> projection1[i]
    image[i] -> random augmentation 2 -> projection -> projection2[i]

    Args:
      projection1: projection vector of shape (bsz, dim).
      projection2: projection vector of shape (bsz, dim).

    Returns:
      A loss scalar.
      The logits for contrastive prediction task.
      The labels for contrastive prediction task.
    """
    # Get (normalized) hidden1 and hidden2.
    if self._projection_norm:
      projection1 = tf.math.l2_normalize(projection1, -1)
      projection2 = tf.math.l2_normalize(projection2, -1)
    batch_size = tf.shape(projection1)[0]

    # Gather projection1/projection2 across replicas and create local labels.
    num_replicas_in_sync = tf.distribute.get_strategy().num_replicas_in_sync
    if num_replicas_in_sync > 1:
      p1_large = cross_replica_concat(projection1, num_replicas_in_sync)
      p2_large = cross_replica_concat(projection2, num_replicas_in_sync)
      enlarged_batch_size = tf.shape(p1_large)[0]
      # TODO(iamtingchen): more elegant way to convert u32 to s32 for replica_id.
      replica_context = tf.distribute.get_replica_context()
      replica_id = tf.cast(
          tf.cast(replica_context.replica_id_in_sync_group, tf.uint32),
          tf.int32)
      labels_idx = tf.range(batch_size) + replica_id * batch_size
      labels = tf.one_hot(labels_idx, enlarged_batch_size * 2)
      masks = tf.one_hot(labels_idx, enlarged_batch_size)
    else:
      p1_large = projection1
      p2_large = projection2
      labels = tf.one_hot(tf.range(batch_size), batch_size * 2)
      masks = tf.one_hot(tf.range(batch_size), batch_size)

    logits_aa = tf.matmul(projection1, p1_large,
                          transpose_b=True) / self._temperature
    logits_aa = logits_aa - masks * LARGE_NUM
    logits_bb = tf.matmul(projection2, p2_large,
                          transpose_b=True) / self._temperature
    logits_bb = logits_bb - masks * LARGE_NUM
    logits_ab = tf.matmul(projection1, p2_large,
                          transpose_b=True) / self._temperature
    logits_ba = tf.matmul(projection2, p1_large,
                          transpose_b=True) / self._temperature

    loss_a = tf.nn.softmax_cross_entropy_with_logits(
        labels, tf.concat([logits_ab, logits_aa], 1))
    loss_b = tf.nn.softmax_cross_entropy_with_logits(
        labels, tf.concat([logits_ba, logits_bb], 1))
    loss = tf.reduce_mean(loss_a + loss_b)

    return loss