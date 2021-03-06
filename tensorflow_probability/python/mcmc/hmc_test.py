# Copyright 2018 The TensorFlow Probability Authors.
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
# ============================================================================
"""Tests for HamiltonianMonteCarlo."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
# Dependency imports
import numpy as np
from scipy import stats

import tensorflow as tf
import tensorflow_probability as tfp

from tensorflow_probability.python.mcmc.hmc import _compute_log_acceptance_correction
from tensorflow_probability.python.mcmc.hmc import _leapfrog_integrator_one_step
from tensorflow_probability.python.mcmc.util import maybe_call_fn_and_grads
from tensorflow.contrib import eager as tfe
from tensorflow.python.eager import context
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import test_util


tfd = tfp.distributions


# Arguments kept to match counterpart,
# `@test_util.run_in_graph_and_eager_modes`.
def run_in_graph_mode_only(__unused__=None, config=None, use_gpu=True):  # pylint: disable=invalid-name,unused-argument
  """Execute the decorated test in graph mode only."""
  assert not __unused__, 'Add () after run_in_graph_mode_only.'
  def decorator(f):
    def decorated(self, **kwargs):
      with context.graph_mode():
        with self.test_session(use_gpu=use_gpu):
          f(self, **kwargs)
    return decorated
  return decorator


def _set_seed(seed):
  """Helper which uses graph seed if using TFE."""
  # TODO(b/68017812): Deprecate once TFE supports seed.
  if tfe.executing_eagerly():
    tf.set_random_seed(seed)
    return None
  return seed


def _reduce_variance(x, axis=None, keepdims=False):
  sample_mean = tf.reduce_mean(x, axis, keepdims=True)
  return tf.reduce_mean(
      tf.squared_difference(x, sample_mean), axis, keepdims)


class HMCTest(tf.test.TestCase):

  def setUp(self):
    self._shape_param = 5.
    self._rate_param = 10.

    random_seed.set_random_seed(10003)
    np.random.seed(10003)

  def assertAllFinite(self, x):
    self.assertAllEqual(np.ones_like(x).astype(bool), np.isfinite(x))

  def _log_gamma_log_prob(self, x, event_dims=()):
    """Computes log-pdf of a log-gamma random variable.

    Args:
      x: Value of the random variable.
      event_dims: Dimensions not to treat as independent.

    Returns:
      log_prob: The log-pdf up to a normalizing constant.
    """
    return tf.reduce_sum(self._shape_param * x -
                         self._rate_param * tf.exp(x),
                         axis=event_dims)

  def _integrator_conserves_energy(self, x, independent_chain_ndims):
    event_dims = tf.range(independent_chain_ndims, tf.rank(x))

    m = tf.random_normal(tf.shape(x))
    log_prob_0, grad_0 = maybe_call_fn_and_grads(
        lambda x: self._log_gamma_log_prob(x, event_dims),
        x)
    old_energy = -log_prob_0 + 0.5 * tf.reduce_sum(m**2., event_dims)

    x_shape = self.evaluate(x).shape
    event_size = np.prod(x_shape[independent_chain_ndims:])
    step_size = tf.constant(0.1 / event_size, x.dtype)
    hmc_lf_steps = tf.constant(1000, np.int32)

    def leapfrog_one_step(*args):
      return _leapfrog_integrator_one_step(
          lambda x: self._log_gamma_log_prob(x, event_dims),
          independent_chain_ndims,
          [step_size],
          *args)

    [[new_m], _, log_prob_1, _] = tf.while_loop(
        cond=lambda *args: True,
        body=leapfrog_one_step,
        loop_vars=[
            [m],         # current_momentum_parts
            [x],         # current_state_parts,
            log_prob_0,  # current_target_log_prob
            grad_0,      # current_target_log_prob_grad_parts
        ],
        maximum_iterations=hmc_lf_steps)

    new_energy = -log_prob_1 + 0.5 * tf.reduce_sum(new_m**2., axis=event_dims)

    old_energy_, new_energy_ = self.evaluate([old_energy, new_energy])
    tf.logging.vlog(1, 'average energy relative change: {}'.format(
        (1. - new_energy_ / old_energy_).mean()))
    self.assertAllClose(old_energy_, new_energy_, atol=0., rtol=0.02)

  def _integrator_conserves_energy_wrapper(self, independent_chain_ndims):
    """Tests the long-term energy conservation of the leapfrog integrator.

    The leapfrog integrator is symplectic, so for sufficiently small step
    sizes it should be possible to run it more or less indefinitely without
    the energy of the system blowing up or collapsing.

    Args:
      independent_chain_ndims: Python `int` scalar representing the number of
        dims associated with independent chains.
    """
    x = tf.constant(np.random.rand(50, 10, 2), np.float32)
    self._integrator_conserves_energy(x, independent_chain_ndims)

  @test_util.run_in_graph_and_eager_modes()
  def testIntegratorEnergyConservationNullShape(self):
    self._integrator_conserves_energy_wrapper(0)

  @test_util.run_in_graph_and_eager_modes()
  def testIntegratorEnergyConservation1(self):
    self._integrator_conserves_energy_wrapper(1)

  @test_util.run_in_graph_and_eager_modes()
  def testIntegratorEnergyConservation2(self):
    self._integrator_conserves_energy_wrapper(2)

  @test_util.run_in_graph_and_eager_modes()
  def testIntegratorEnergyConservation3(self):
    self._integrator_conserves_energy_wrapper(3)

  @test_util.run_in_graph_and_eager_modes()
  def testSampleChainSeedReproducibleWorksCorrectly(self):
    num_results = 10
    independent_chain_ndims = 1

    def log_gamma_log_prob(x):
      event_dims = tf.range(independent_chain_ndims, tf.rank(x))
      return self._log_gamma_log_prob(x, event_dims)

    current_state = np.random.rand(4, 3, 2)

    samples0, kernel_results0 = tfp.mcmc.sample_chain(
        num_results=2 * num_results,
        num_steps_between_results=0,
        # Following args are identical to below.
        current_state=current_state,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=log_gamma_log_prob,
            step_size=0.1,
            num_leapfrog_steps=2,
            seed=_set_seed(52)),
        num_burnin_steps=150,
        parallel_iterations=1)

    samples1, kernel_results1 = tfp.mcmc.sample_chain(
        num_results=num_results,
        num_steps_between_results=1,
        # Following args are identical to above.
        current_state=current_state,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=log_gamma_log_prob,
            step_size=0.1,
            num_leapfrog_steps=2,
            seed=_set_seed(52)),
        num_burnin_steps=150,
        parallel_iterations=1)

    [
        samples0_,
        samples1_,
        target_log_prob0_,
        target_log_prob1_,
    ] = self.evaluate([
        samples0,
        samples1,
        kernel_results0.accepted_results.target_log_prob,
        kernel_results1.accepted_results.target_log_prob,
    ])
    self.assertAllClose(samples0_[::2], samples1_,
                        atol=1e-5, rtol=1e-5)
    self.assertAllClose(target_log_prob0_[::2], target_log_prob1_,
                        atol=1e-5, rtol=1e-5)

  def _chain_gets_correct_expectations(self, x, independent_chain_ndims):
    counter = collections.Counter()
    def log_gamma_log_prob(x):
      counter['target_calls'] += 1
      event_dims = tf.range(independent_chain_ndims, tf.rank(x))
      return self._log_gamma_log_prob(x, event_dims)

    samples, kernel_results = tfp.mcmc.sample_chain(
        num_results=150,
        current_state=x,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=log_gamma_log_prob,
            step_size=0.05,
            num_leapfrog_steps=2,
            seed=_set_seed(42)),
        num_burnin_steps=150,
        parallel_iterations=1)

    if tfe.executing_eagerly():
      # TODO(b/79991421): Figure out why this is approx twice as many as it
      # should be. I.e., `expected_calls = (150 + 150) * 2 + 1`.
      expected_calls = 1202
    else:
      expected_calls = 2
    self.assertAllEqual(dict(target_calls=expected_calls), counter)

    expected_x = (tf.digamma(self._shape_param)
                  - np.log(self._rate_param))

    expected_exp_x = self._shape_param / self._rate_param

    log_accept_ratio_, samples_, expected_x_ = self.evaluate(
        [kernel_results.log_accept_ratio, samples, expected_x])

    actual_x = samples_.mean()
    actual_exp_x = np.exp(samples_).mean()
    acceptance_probs = np.exp(np.minimum(log_accept_ratio_, 0.))

    tf.logging.vlog(1, 'True      E[x, exp(x)]: {}\t{}'.format(
        expected_x_, expected_exp_x))
    tf.logging.vlog(1, 'Estimated E[x, exp(x)]: {}\t{}'.format(
        actual_x, actual_exp_x))
    self.assertNear(actual_x, expected_x_, 2e-2)
    self.assertNear(actual_exp_x, expected_exp_x, 2e-2)
    self.assertAllEqual(np.ones_like(acceptance_probs, np.bool),
                        acceptance_probs > 0.5)
    self.assertAllEqual(np.ones_like(acceptance_probs, np.bool),
                        acceptance_probs <= 1.)

  def _chain_gets_correct_expectations_wrapper(self, independent_chain_ndims):
    x = tf.constant(np.random.rand(50, 10, 2), np.float32, name='x')
    self._chain_gets_correct_expectations(x, independent_chain_ndims)

  @test_util.run_in_graph_and_eager_modes()
  def testHMCChainExpectationsNullShape(self):
    self._chain_gets_correct_expectations_wrapper(0)

  @test_util.run_in_graph_and_eager_modes()
  def testHMCChainExpectations1(self):
    self._chain_gets_correct_expectations_wrapper(1)

  @test_util.run_in_graph_and_eager_modes()
  def testHMCChainExpectations2(self):
    self._chain_gets_correct_expectations_wrapper(2)

  @test_util.run_in_graph_and_eager_modes()
  def testKernelResultsUsingTruncatedDistribution(self):
    def log_prob(x):
      return tf.where(
          x >= 0.,
          -x - x**2,  # Non-constant gradient.
          tf.fill(x.shape, tf.cast(-np.inf, x.dtype)))
    # This log_prob has the property that it is likely to attract
    # the flow toward, and below, zero...but for x <=0,
    # log_prob(x) = -inf, which should result in rejection, as well
    # as a non-finite log_prob.  Thus, this distribution gives us an opportunity
    # to test out the kernel results ability to correctly capture rejections due
    # to finite AND non-finite reasons.
    # Why use a non-constant gradient?  This ensures the leapfrog integrator
    # will not be exact.

    num_results = 1000
    # Large step size, will give rejections due to integration error in addition
    # to rejection due to going into a region of log_prob = -inf.
    step_size = 0.2
    num_leapfrog_steps = 5
    num_chains = 2

    # Start multiple independent chains.
    initial_state = tf.convert_to_tensor([0.1] * num_chains)

    states, kernel_results = tfp.mcmc.sample_chain(
        num_results=num_results,
        current_state=initial_state,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=log_prob,
            step_size=step_size,
            num_leapfrog_steps=num_leapfrog_steps,
            seed=_set_seed(42)),
        parallel_iterations=1)

    states_, kernel_results_ = self.evaluate([states, kernel_results])
    pstates_ = kernel_results_.proposed_state

    neg_inf_mask = np.isneginf(
        kernel_results_.proposed_results.target_log_prob)

    # First:  Test that the mathematical properties of the above log prob
    # function in conjunction with HMC show up as expected in kernel_results_.

    # We better have log_prob = -inf some of the time.
    self.assertLess(0, neg_inf_mask.sum())
    # We better have some rejections due to something other than -inf.
    self.assertLess(neg_inf_mask.sum(), (~kernel_results_.is_accepted).sum())
    # We better have accepted a decent amount, even near end of the chain.
    self.assertLess(
        0.1, kernel_results_.is_accepted[int(0.9 * num_results):].mean())
    # We better not have any NaNs in states or log_prob.
    # We may have some NaN in grads, which involve multiplication/addition due
    # to gradient rules.  This is the known "NaN grad issue with tf.where."
    self.assertAllEqual(
        np.zeros_like(states_),
        np.isnan(kernel_results_.proposed_results.target_log_prob))
    self.assertAllEqual(
        np.zeros_like(states_),
        np.isnan(states_))
    # We better not have any +inf in states, grads, or log_prob.
    self.assertAllEqual(
        np.zeros_like(states_),
        np.isposinf(kernel_results_.proposed_results.target_log_prob))
    self.assertAllEqual(
        np.zeros_like(states_),
        np.isposinf(
            kernel_results_.proposed_results.grads_target_log_prob[0]))
    self.assertAllEqual(np.zeros_like(states_),
                        np.isposinf(states_))

    # Second:  Test that kernel_results is congruent with itself and
    # acceptance/rejection of states.

    # Proposed state is negative iff proposed target log prob is -inf.
    np.testing.assert_array_less(pstates_[neg_inf_mask], 0.)
    np.testing.assert_array_less(0., pstates_[~neg_inf_mask])

    # Acceptance probs are zero whenever proposed state is negative.
    acceptance_probs = np.exp(np.minimum(
        kernel_results_.log_accept_ratio, 0.))
    self.assertAllEqual(
        np.zeros_like(pstates_[neg_inf_mask]),
        acceptance_probs[neg_inf_mask])

    # The move is accepted ==> state = proposed state.
    self.assertAllEqual(
        states_[kernel_results_.is_accepted],
        pstates_[kernel_results_.is_accepted],
    )
    # The move was rejected <==> state[t] == state[t - 1].
    for t in range(1, num_results):
      for i in range(num_chains):
        if kernel_results_.is_accepted[t, i]:
          self.assertNotEqual(states_[t, i], states_[t - 1, i])
        else:
          self.assertEqual(states_[t, i], states_[t - 1, i])

  def _kernel_leaves_target_invariant(self, initial_draws,
                                      independent_chain_ndims):
    def log_gamma_log_prob(x):
      event_dims = tf.range(independent_chain_ndims, tf.rank(x))
      return self._log_gamma_log_prob(x, event_dims)

    def fake_log_prob(x):
      """Cooled version of the target distribution."""
      return 1.1 * log_gamma_log_prob(x)

    hmc = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=log_gamma_log_prob,
        step_size=0.4,
        num_leapfrog_steps=5,
        seed=_set_seed(43))
    sample, kernel_results = hmc.one_step(
        current_state=initial_draws,
        previous_kernel_results=hmc.bootstrap_results(initial_draws))

    bad_hmc = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=fake_log_prob,
        step_size=0.4,
        num_leapfrog_steps=5,
        seed=_set_seed(44))
    bad_sample, bad_kernel_results = bad_hmc.one_step(
        current_state=initial_draws,
        previous_kernel_results=bad_hmc.bootstrap_results(initial_draws))

    [
        log_accept_ratio_,
        bad_log_accept_ratio_,
        initial_draws_,
        updated_draws_,
        fake_draws_,
    ] = self.evaluate([
        kernel_results.log_accept_ratio,
        bad_kernel_results.log_accept_ratio,
        initial_draws,
        sample,
        bad_sample,
    ])

    # Confirm step size is small enough that we usually accept.
    acceptance_probs = np.exp(np.minimum(log_accept_ratio_, 0.))
    bad_acceptance_probs = np.exp(np.minimum(bad_log_accept_ratio_, 0.))
    self.assertGreater(acceptance_probs.mean(), 0.5)
    self.assertGreater(bad_acceptance_probs.mean(), 0.5)

    # Confirm step size is large enough that we sometimes reject.
    self.assertLess(acceptance_probs.mean(), 0.99)
    self.assertLess(bad_acceptance_probs.mean(), 0.99)

    _, ks_p_value_true = stats.ks_2samp(initial_draws_.flatten(),
                                        updated_draws_.flatten())
    _, ks_p_value_fake = stats.ks_2samp(initial_draws_.flatten(),
                                        fake_draws_.flatten())

    tf.logging.vlog(1, 'acceptance rate for true target: {}'.format(
        acceptance_probs.mean()))
    tf.logging.vlog(1, 'acceptance rate for fake target: {}'.format(
        bad_acceptance_probs.mean()))
    tf.logging.vlog(1, 'K-S p-value for true target: {}'.format(
        ks_p_value_true))
    tf.logging.vlog(1, 'K-S p-value for fake target: {}'.format(
        ks_p_value_fake))
    # Make sure that the MCMC update hasn't changed the empirical CDF much.
    self.assertGreater(ks_p_value_true, 1e-3)
    # Confirm that targeting the wrong distribution does
    # significantly change the empirical CDF.
    self.assertLess(ks_p_value_fake, 1e-6)

  def _kernel_leaves_target_invariant_wrapper(self, independent_chain_ndims):
    """Tests that the kernel leaves the target distribution invariant.

    Draws some independent samples from the target distribution,
    applies an iteration of the MCMC kernel, then runs a
    Kolmogorov-Smirnov test to determine if the distribution of the
    MCMC-updated samples has changed.

    We also confirm that running the kernel with a different log-pdf
    does change the target distribution. (And that we can detect that.)

    Args:
      independent_chain_ndims: Python `int` scalar representing the number of
        dims associated with independent chains.
    """
    initial_draws = np.log(np.random.gamma(self._shape_param,
                                           size=[50000, 2, 2]))
    initial_draws -= np.log(self._rate_param)
    x = tf.constant(initial_draws, np.float32)
    self._kernel_leaves_target_invariant(x, independent_chain_ndims)

  @test_util.run_in_graph_and_eager_modes()
  def testKernelLeavesTargetInvariant1(self):
    self._kernel_leaves_target_invariant_wrapper(1)

  @test_util.run_in_graph_and_eager_modes()
  def testKernelLeavesTargetInvariant2(self):
    self._kernel_leaves_target_invariant_wrapper(2)

  @test_util.run_in_graph_and_eager_modes()
  def testKernelLeavesTargetInvariant3(self):
    self._kernel_leaves_target_invariant_wrapper(3)

  @test_util.run_in_graph_and_eager_modes()
  def testNanRejection(self):
    """Tests that an update that yields NaN potentials gets rejected.

    We run HMC with a target distribution that returns NaN
    log-likelihoods if any element of x < 0, and unit-scale
    exponential log-likelihoods otherwise. The exponential potential
    pushes x towards 0, ensuring that any reasonably large update will
    push us over the edge into NaN territory.
    """
    def _unbounded_exponential_log_prob(x):
      """An exponential distribution with log-likelihood NaN for x < 0."""
      per_element_potentials = tf.where(
          x < 0.,
          tf.fill(tf.shape(x), x.dtype.as_numpy_dtype(np.nan)),
          -x)
      return tf.reduce_sum(per_element_potentials)

    initial_x = tf.linspace(0.01, 5, 10)
    hmc = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=_unbounded_exponential_log_prob,
        step_size=2.,
        num_leapfrog_steps=5,
        seed=_set_seed(46))
    updated_x, kernel_results = hmc.one_step(
        current_state=initial_x,
        previous_kernel_results=hmc.bootstrap_results(initial_x))
    initial_x_, updated_x_, log_accept_ratio_ = self.evaluate(
        [initial_x, updated_x, kernel_results.log_accept_ratio])
    acceptance_probs = np.exp(np.minimum(log_accept_ratio_, 0.))

    tf.logging.vlog(1, 'initial_x = {}'.format(initial_x_))
    tf.logging.vlog(1, 'updated_x = {}'.format(updated_x_))
    tf.logging.vlog(1, 'log_accept_ratio = {}'.format(log_accept_ratio_))

    self.assertAllEqual(initial_x_, updated_x_)
    self.assertEqual(acceptance_probs, 0.)

  @run_in_graph_mode_only()
  def testNanFromGradsDontPropagate(self):
    """Test that update with NaN gradients does not cause NaN in results."""
    def _nan_log_prob_with_nan_gradient(x):
      return np.nan * tf.reduce_sum(x)

    initial_x = tf.linspace(0.01, 5, 10)
    hmc = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=_nan_log_prob_with_nan_gradient,
        step_size=2.,
        num_leapfrog_steps=5,
        seed=_set_seed(47))
    updated_x, kernel_results = hmc.one_step(
        current_state=initial_x,
        previous_kernel_results=hmc.bootstrap_results(initial_x))
    initial_x_, updated_x_, log_accept_ratio_ = self.evaluate(
        [initial_x, updated_x, kernel_results.log_accept_ratio])
    acceptance_probs = np.exp(np.minimum(log_accept_ratio_, 0.))

    tf.logging.vlog(1, 'initial_x = {}'.format(initial_x_))
    tf.logging.vlog(1, 'updated_x = {}'.format(updated_x_))
    tf.logging.vlog(1, 'log_accept_ratio = {}'.format(log_accept_ratio_))

    self.assertAllEqual(initial_x_, updated_x_)
    self.assertEqual(acceptance_probs, 0.)

    self.assertAllFinite(
        tf.gradients(updated_x, initial_x)[0].eval())
    self.assertAllEqual(
        [True],
        [g is None for g in tf.gradients(
            kernel_results.proposed_results.grads_target_log_prob,
            initial_x)])

    # Gradients of the acceptance probs and new log prob are not finite.
    # self.assertAllFinite(
    #     tf.gradients(acceptance_probs, initial_x)[0].eval())
    # self.assertAllFinite(
    #     tf.gradients(new_log_prob, initial_x)[0].eval())

  def _testChainWorksDtype(self, dtype):
    states, kernel_results = tfp.mcmc.sample_chain(
        num_results=10,
        current_state=np.zeros(5).astype(dtype),
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=lambda x: -tf.reduce_sum(x**2., axis=-1),
            step_size=0.01,
            num_leapfrog_steps=10,
            seed=_set_seed(48)),
        parallel_iterations=1)
    states_, log_accept_ratio_ = self.evaluate(
        [states, kernel_results.log_accept_ratio])
    self.assertEqual(dtype, states_.dtype)
    self.assertEqual(dtype, log_accept_ratio_.dtype)

  @test_util.run_in_graph_and_eager_modes()
  def testChainWorksIn64Bit(self):
    self._testChainWorksDtype(np.float64)

  @test_util.run_in_graph_and_eager_modes()
  def testChainWorksIn16Bit(self):
    self._testChainWorksDtype(np.float16)

  @test_util.run_in_graph_and_eager_modes()
  def testChainWorksCorrelatedMultivariate(self):
    dtype = np.float32
    true_mean = dtype([0, 0])
    true_cov = dtype([[1, 0.5],
                      [0.5, 1]])
    num_results = 1500
    counter = collections.Counter()
    def target_log_prob(x, y):
      counter['target_calls'] += 1
      # Corresponds to unnormalized MVN.
      # z = matmul(inv(chol(true_cov)), [x, y] - true_mean)
      z = tf.stack([x, y], axis=-1) - true_mean
      z = tf.squeeze(
          tf.linalg.triangular_solve(
              np.linalg.cholesky(true_cov),
              z[..., tf.newaxis]),
          axis=-1)
      return -0.5 * tf.reduce_sum(z**2., axis=-1)
    states, kernel_results = tfp.mcmc.sample_chain(
        num_results=num_results,
        current_state=[dtype(-2), dtype(2)],
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=target_log_prob,
            step_size=[1.23, 1.23],
            num_leapfrog_steps=2,
            seed=_set_seed(54)),
        num_burnin_steps=200,
        parallel_iterations=1)

    if tfe.executing_eagerly():
      # TODO(b/79991421): Figure out why this is approx twice as many as it
      # should be. I.e., `expected_calls = (num_results + 200) * 2 * 2 + 1`.
      expected_calls = 6802
    else:
      expected_calls = 2
    self.assertAllEqual(dict(target_calls=expected_calls), counter)

    states = tf.stack(states, axis=-1)
    self.assertEqual(num_results, states.shape[0].value)
    sample_mean = tf.reduce_mean(states, axis=0)
    x = states - sample_mean
    sample_cov = tf.matmul(x, x, transpose_a=True) / dtype(num_results)
    [sample_mean_, sample_cov_, is_accepted_] = self.evaluate([
        sample_mean, sample_cov, kernel_results.is_accepted])
    self.assertNear(0.6, is_accepted_.mean(), err=0.05)
    self.assertAllClose(true_mean, sample_mean_,
                        atol=0.06, rtol=0.)
    self.assertAllClose(true_cov, sample_cov_,
                        atol=0., rtol=0.2)

  @test_util.run_in_graph_and_eager_modes()
  def testUncalibratedHMCPreservesStaticShape(self):
    uncal_hmc = tfp.mcmc.UncalibratedHamiltonianMonteCarlo(
        target_log_prob_fn=lambda x: tf.reduce_sum(-x**2., axis=-1),
        step_size=0.5,
        num_leapfrog_steps=2,
        seed=_set_seed(1042))
    x0 = tf.constant([[-1., 0.5],
                      [0., 0.],
                      [1., 1.25]])
    r0 = uncal_hmc.bootstrap_results(x0)
    x1, r1 = uncal_hmc.one_step(x0, r0)
    self.assertAllEqual([3, 2], x0.shape)
    self.assertAllEqual([3], r0.target_log_prob.shape)
    self.assertAllEqual([3, 2], x1.shape)
    self.assertAllEqual([3], r1.target_log_prob.shape)

  @test_util.run_in_graph_and_eager_modes()
  def testHMCPreservesStaticShape(self):
    hmc = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=lambda x: tf.reduce_sum(-x**2., axis=-1),
        step_size=0.5,
        num_leapfrog_steps=2,
        seed=_set_seed(1042))
    x0 = tf.constant([[-1., 0.5],
                      [0., 0.],
                      [1., 1.25]])
    r0 = hmc.bootstrap_results(x0)
    x1, r1 = hmc.one_step(x0, r0)
    self.assertAllEqual([3, 2], x0.shape)
    self.assertAllEqual([3], r0.accepted_results.target_log_prob.shape)
    self.assertAllEqual([3, 2], x1.shape)
    self.assertAllEqual([3], r1.accepted_results.target_log_prob.shape)


class _LogCorrectionTest(object):

  @test_util.run_in_graph_and_eager_modes()
  def testHandlesNanFromPotential(self):
    tlp = [1, np.inf, -np.inf, np.nan]
    target_log_prob, proposed_target_log_prob = [
        self.dtype(x.flatten()) for x in np.meshgrid(tlp, tlp)]
    num_chains = len(target_log_prob)
    x0 = np.zeros(num_chains, dtype=self.dtype)

    def make_trick_fun(f):
      f_x = tf.convert_to_tensor(f)
      def _fn(x):
        # We'll make the gradient be `1` regardless of input.
        return f_x + (x - tf.stop_gradient(x))
      return _fn

    # Use trick fun to get "current" results.
    pkr = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=make_trick_fun(target_log_prob),
        step_size=1.,
        num_leapfrog_steps=1).bootstrap_results(x0)

    # Use trick fun to inject "proposed" results.
    _, results = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=make_trick_fun(proposed_target_log_prob),
        step_size=1.,
        num_leapfrog_steps=1).one_step(x0, pkr)

    [actual_log_accept_ratio_, actual_grads_target_log_prob_] = self.evaluate([
        results.log_accept_ratio,
        results.accepted_results.grads_target_log_prob])

    # First log(accept_ratio) is finite, rest are weird so reject them.
    self.assertTrue(np.isfinite(actual_log_accept_ratio_[0]))
    self.assertAllEqual(self.dtype([-np.inf]*(num_chains - 1)),
                        actual_log_accept_ratio_[1:])

    # Ensure gradient is finite.
    self.assertAllEqual(
        np.ones_like(actual_grads_target_log_prob_).astype(np.bool),
        np.isfinite(actual_grads_target_log_prob_))

  @run_in_graph_mode_only()
  def testHandlesNanFromKinetic(self):
    x = [1, np.inf, -np.inf, np.nan]
    momentums, proposed_momentums = [
        [np.reshape(self.dtype(x), [-1, 1])]
        for x in np.meshgrid(x, x)]
    num_chains = len(momentums[0])

    momentums = [tf.convert_to_tensor(momentums[0])]
    proposed_momentums = [tf.convert_to_tensor(proposed_momentums[0])]

    log_acceptance_correction = _compute_log_acceptance_correction(
        momentums,
        proposed_momentums,
        independent_chain_ndims=1)
    grads = tf.gradients(log_acceptance_correction, momentums)

    [actual_log_acceptance_correction, grads_] = self.evaluate([
        log_acceptance_correction, grads])

    # Ensure log_acceptance_correction is `inf` (note: that's positive inf) in
    # weird cases and finite otherwise.
    expected_log_acceptance_correction = -(
        self.dtype([0] + [np.inf]*(num_chains - 1)))
    self.assertAllEqual(expected_log_acceptance_correction,
                        actual_log_acceptance_correction)

    # Ensure gradient is finite.
    g = grads_[0].reshape([len(x), len(x)])[:, 0]
    self.assertAllEqual(np.ones_like(g).astype(np.bool), np.isfinite(g))

    # The remaining gradients are nan because the momentum was itself nan or
    # inf.
    g = grads_[0].reshape([len(x), len(x)])[:, 1:]
    self.assertAllEqual(np.ones_like(g).astype(np.bool), np.isnan(g))


class LogCorrectionTest16(tf.test.TestCase, _LogCorrectionTest):
  dtype = np.float16


class LogCorrectionTest32(tf.test.TestCase, _LogCorrectionTest):
  dtype = np.float32


class LogCorrectionTest64(tf.test.TestCase, _LogCorrectionTest):
  dtype = np.float64


class _HMCHandlesLists(object):

  @test_util.run_in_graph_and_eager_modes()
  def testStateParts(self):
    cast = lambda x: np.array(x, self.dtype)
    dist_x = tfd.Normal(loc=cast(0), scale=cast(1))
    dist_y = tfd.Independent(
        tfd.Gamma(concentration=cast([1, 2]),
                  rate=cast([0.5, 0.75])),
        reinterpreted_batch_ndims=1)
    def target_log_prob(x, y):
      return dist_x.log_prob(x) + dist_y.log_prob(y)
    x0 = [dist_x.sample(seed=_set_seed(61)), dist_y.sample(seed=_set_seed(62))]
    samples, kernel_results = tfp.mcmc.sample_chain(
        num_results=1500,
        current_state=x0,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=target_log_prob,
            step_size=0.5,
            num_leapfrog_steps=3,
            seed=_set_seed(49)),
        num_burnin_steps=500,
        parallel_iterations=1)
    actual_means = [tf.reduce_mean(s, axis=0) for s in samples]
    actual_vars = [_reduce_variance(s, axis=0) for s in samples]
    expected_means = [dist_x.mean(), dist_y.mean()]
    expected_vars = [dist_x.variance(), dist_y.variance()]
    [
        actual_means_,
        actual_vars_,
        expected_means_,
        expected_vars_,
        is_accepted_,
    ] = self.evaluate([
        actual_means,
        actual_vars,
        expected_means,
        expected_vars,
        kernel_results.is_accepted,
    ])
    # Assert acceptance rate is asymptotically optimal.
    self.assertNear(0.651, np.mean(is_accepted_), err=0.05)
    self.assertAllClose(expected_means_, actual_means_, atol=0.07, rtol=0.16)
    self.assertAllClose(expected_vars_, actual_vars_, atol=0., rtol=0.5)


class HMCHandlesLists32(_HMCHandlesLists, tf.test.TestCase):
  dtype = np.float32


class HMCHandlesLists64(_HMCHandlesLists, tf.test.TestCase):
  dtype = np.float64


class HMCEMAdaptiveStepSize(tf.test.TestCase):
  """This test verifies that the docstring example works as advertised."""

  def setUp(self):
    random_seed.set_random_seed(10014)
    np.random.seed(10014)

  def make_training_data(self, num_samples, dims, sigma):
    dt = np.asarray(sigma).dtype
    zeros = tf.zeros(dims, dtype=dt)
    x = tf.transpose(tfd.MultivariateNormalDiag(loc=zeros).sample(
        num_samples, seed=1))  # [d, n]
    w = tfd.MultivariateNormalDiag(
        loc=zeros,
        scale_identity_multiplier=sigma).sample([1], seed=2)  # [1, d]
    noise = tfd.Normal(loc=np.array(0, dt), scale=np.array(1, dt)).sample(
        num_samples, seed=3)  # [n]
    y = tf.matmul(w, x) + noise  # [1, n]
    return y[0], x, w[0]

  def make_weights_prior(self, dims, dtype):
    return tfd.MultivariateNormalDiag(
        loc=tf.zeros([dims], dtype=dtype),
        scale_identity_multiplier=tf.exp(tf.get_variable(
            name='log_sigma',
            initializer=np.array(0, dtype),
            use_resource=True)))

  def make_response_likelihood(self, w, x):
    w_shape = tf.pad(
        tf.shape(w),
        paddings=[[tf.where(tf.rank(w) > 1, 0, 1), 0]],
        constant_values=1)
    y_shape = tf.concat([tf.shape(w)[:-1], [tf.shape(x)[-1]]], axis=0)
    w_expand = tf.reshape(w, w_shape)
    return tfd.Normal(
        loc=tf.reshape(tf.matmul(w_expand, x), y_shape),
        scale=np.array(1, w.dtype.as_numpy_dtype))  # [n]

  def test_mcem_converges(self):
    # Setup assumptions.
    dtype = np.float32
    num_samples = 500
    dims = 10

    weights_prior_true_scale = np.array(0.3, dtype)
    with tf.Session() as sess:
      y, x, _ = sess.run(
          self.make_training_data(num_samples, dims, weights_prior_true_scale))

    prior = self.make_weights_prior(dims, dtype)
    def unnormalized_posterior_log_prob(w):
      likelihood = self.make_response_likelihood(w, x)
      return (prior.log_prob(w)
              + tf.reduce_sum(likelihood.log_prob(y), axis=-1))  # [m]

    weights_chain_start = tf.placeholder(dtype, shape=[dims])

    step_size = tf.get_variable(
        name='step_size',
        initializer=np.array(0.05, dtype),
        use_resource=True,
        trainable=False)

    num_results = 2
    weights, kernel_results = tfp.mcmc.sample_chain(
        num_results=num_results,
        num_burnin_steps=0,
        current_state=weights_chain_start,
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=unnormalized_posterior_log_prob,
            num_leapfrog_steps=2,
            step_size=step_size,
            step_size_update_fn=tfp.mcmc.step_size_simple_update,
            state_gradients_are_stopped=True,
            seed=_set_seed(252)),
        parallel_iterations=1)

    avg_acceptance_ratio = tf.reduce_mean(
        tf.exp(tf.minimum(kernel_results.log_accept_ratio, 0.)))

    # We do an optimization step to propagate `log_sigma` after two HMC steps to
    # propagate `weights`.
    loss = -tf.reduce_mean(kernel_results.accepted_results.target_log_prob)
    optimizer = tf.train.GradientDescentOptimizer(learning_rate=0.01)
    train_op = optimizer.minimize(loss)

    with tf.variable_scope(tf.get_variable_scope(), reuse=True):
      weights_prior_estimated_scale = tf.exp(
          tf.get_variable(name='log_sigma', dtype=dtype))

    init_op = tf.global_variables_initializer()

    num_iters = int(40)

    weights_prior_estimated_scale_ = np.zeros(num_iters, dtype)
    weights_ = np.zeros([num_iters + 1, dims], dtype)
    loss_ = np.zeros([num_iters], dtype)
    weights_[0] = np.random.randn(dims).astype(dtype)

    with tf.Session() as sess:
      init_op.run()
      for iter_ in range(num_iters):
        [
            _,
            weights_prior_estimated_scale_[iter_],
            weights_[iter_ + 1],
            loss_[iter_],
            step_size_,
            avg_acceptance_ratio_,
        ] = sess.run([
            train_op,
            weights_prior_estimated_scale,
            weights[-1],
            loss,
            step_size,
            avg_acceptance_ratio,
        ], feed_dict={weights_chain_start: weights_[iter_]})
        # Enable using bazel flags:
        # `--test_arg="--logtostderr" --test_arg="--vmodule=hmc_test=2"`,
        # E.g.,
        # bazel test --test_output=streamed -c opt :hmc_test \
        # --test_filter=HMCEMAdaptiveStepSize \
        # --test_arg="--logtostderr" --test_arg="--vmodule=hmc_test=2"
        tf.logging.vlog(
            1, ('iter:{:>2}  loss:{: 9.3f}  scale:{:.3f}  '
                'step_size:{:.4f}  avg_acceptance_ratio:{:.4f}').format(
                    iter_, loss_[iter_], weights_prior_estimated_scale_[iter_],
                    step_size_, avg_acceptance_ratio_))

    # Loss had better decrease....
    self.assertGreater(loss_[:10].mean(), loss_[-10:].mean())
    self.assertNear(0.24,  # Actually smaller than weights_prior_true_scale,
                    weights_prior_estimated_scale_[-5:].mean(),
                    err=0.005)

  @test_util.run_in_graph_and_eager_modes
  def test_step_size_adapts(self):
    dtype = np.float32

    def unnormalized_log_prob(x):
      return -x - x**2

    # TODO(b/111765211): Switch to the following once
    # `get_variable(use_resource=True)` has the same semantics as
    # `tf.contrib.eager.Variable`.
    #   step_size = tf.get_variable(
    #       name='step_size',
    #       initializer=np.array(0.05, dtype),
    #       use_resource=True,
    #       trainable=False)
    step_size = tf.contrib.eager.Variable(
        initial_value=np.array(0.05, dtype),
        name='step_size',
        trainable=False)

    _, kernel_results = tfp.mcmc.sample_chain(
        num_results=int(1e3),
        num_burnin_steps=100,
        current_state=tf.zeros([], dtype),
        kernel=tfp.mcmc.HamiltonianMonteCarlo(
            target_log_prob_fn=unnormalized_log_prob,
            num_leapfrog_steps=2,
            step_size=step_size,
            step_size_update_fn=tfp.mcmc.step_size_simple_update,
            seed=_set_seed(252)),
        parallel_iterations=1)

    init_op = tf.global_variables_initializer()
    self.evaluate(init_op)
    [kernel_results_, step_size_] = self.evaluate([
        kernel_results, kernel_results.extra.step_size_assign])

    # The important thing is that the new step_size does not equal the original,
    # 0.05. However, we're not using `self.assertNotEqual` because testing for
    # `1.25` reveals just how much the step_size has changed.
    self.assertNear(1.25, step_size_[-100:].mean(), err=0.03)
    self.assertNear(0., step_size_[-100:].std(), err=0.04)
    # Anything in [0.6, 0.9] is sufficient. https://arxiv.org/abs/1411.6669
    self.assertNear(0.75, kernel_results_.is_accepted.mean(), err=0.05)


if __name__ == '__main__':
  tf.test.main()
