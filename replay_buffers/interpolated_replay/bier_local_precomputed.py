"""Python class for implementing a queue-based version of the base replay buffer
class as well as GPR. Inherits all attributes from the base replay class, but
overwrites the replay function in addition to the interpolate_samples function.

Namely, this class precomputes predicted samples to take advantage of GPU
acceleration with PyTorch and CUDA. This allows for significant runtime
improvement in training, particularly for high replay ratios. Since the main
objective of BIER is to improve sample efficiency, this replay buffer is
recommended for training agents.

Additionally, this encodes a representation that allows for storing the local
hyperparameters of each model for retraining and re-initializing. Utility
methods have been provided at the end of this module detailing hyperparameter:

    1. Storage (arrays)
    2. Updating after model training (untiling and pushing)
    3. Initializing before model training (tiling and pulling)
"""
# External Python packages
import numpy as np
import torch
import gpytorch
from ray.rllib.policy.sample_batch import SampleBatch, MultiAgentBatch, \
    DEFAULT_POLICY_ID

# Native Python packages
import gc
import time

# Custom Python packages/modules
import utils.gpytorch.gpytorch_utils as gputils
from replay_buffers.interpolated_replay.bier import BIER
from utils.gpytorch.gpytorch_utils import gpr_train_batch, \
    format_preds_precomputed, standardize_features_standardize_targets, \
    normalize_features_standardize_targets
from utils.visualization.visualization import pca_heatmap, pca_1D, \
    plot_compare_linear_gp
from parameters import GP_EPOCHS, GP_LR, GP_THR
import parameters


class PreCompDynamicBIER(BIER):
    """Performs Bayesian Interpolated Experience Replay (BIER) using
    pre-computed samples to improve runtime efficiency of BIER.

    Class for BIER with pre-computed samples. Inherits from BIER.
    Python class for Bayesian Interpolated Experience Replay with
    Gaussian Process Regression-based interpolation, implemented in the library
    GPyTorch. This class performs interpolation by fitting local environment
    models defined over the (s, a, r, s') transition space of the replay buffer.

    *For more notes and recommendations on this model, please check out the
    docstrings in BIER (bier.py).

    The parameters for all types of interpolated_replay experience replay are given
    in ier_base.py, except for the two parameters explicitly included in the
    constructor below.

    Parameters:
        queue_train_size (int): The number of GPR models trained simultaneously.
            Defaults to 256 (typically the training batch size).
        queue_size (int): The maximum number of precomputed samples to store in
            the replay buffer. Defaults to 50.
    """
    def __init__(self, queue_train_size=256, queue_size=50, *args, **kwargs):

        # Call constructor of superclass (BIER)
        super().__init__(*args, **kwargs)

        # Create boolean for whether buffer has been initialized
        self.initialized = False

        # Set queue training size and maximum queue size for precomputing
        self.queue_train_size = queue_train_size
        self.queue_size = queue_size

        # Turn on property for storing hyperparameters
        self.store_hyperparameters = True

    def run_init(self):
        """Method to initialize arrays. Cannot be done in constructor, since
        replay buffer needs to be populated first."""

        # Add integer counters
        self.counter = np.full(
            self.buffer_size, self.queue_size+1).astype(np.int32)

        # Initialize queues for fast indexing
        self.queue_obs = np.zeros(
            (self.buffer_size, self.queue_size, self.d_s))  # Obs arr
        self.queue_actions = np.zeros(
            (self.buffer_size, self.queue_size, self.d_a))  # Actions arr
        self.queue_rewards = np.squeeze(np.zeros(
            (self.buffer_size, self.queue_size, self.d_r)))  # Rewards arr
        self.queue_new_obs = np.zeros(
            (self.buffer_size, self.queue_size, self.d_s))  # New/delta obs arr

        # Initialize queue for convex interpolation
        self.queue_B = np.zeros((self.buffer_size, self.queue_size))  # B arr

        # Queues for likelihood weights and priority updates (set when sampling)
        if self.weighted_updates:
            self.queue_likelihood_weights = np.zeros(
                (self.buffer_size, self.queue_size))  # likelihood weights arr

        # Track neighbor idx for querying priorities
        self.queue_neighbor_idx = np.zeros(
            (self.buffer_size, self.queue_size)).astype(np.int32)  # k arr

        # Set model to "already initialized" state
        self.initialized = True

        # Start storage state as False
        self.hyperparameter_storage_set = False

        # Train on all new data
        total_n_to_train = self.total_sample_count
        train_idx = 0  # Current idx
        i = 0  # Current (integer) iteration of retraining

        # Retrain in batches
        print("TRAINING EXISTING SAMPLES IN BUFFER...")
        while train_idx < total_n_to_train:

            # Number to train in batch
            idx_start = train_idx
            idx_end = min(self.queue_train_size + idx_start, total_n_to_train)
            idx_to_train = np.arange(idx_start, idx_end)

            # Display current counts trained
            print("Training Samples: {}:{}".format(idx_start, idx_end))

            # Retrain models where neighbors change
            self.train_models_and_precompute(
                idx_to_train, batch_size=idx_to_train.size,
                new_point=True, train=True)

            # Increment counter
            train_idx += idx_end - idx_start

    def replay(self):
        """Function for modifying the experiences sampled from the replay buffer
        via interpolation.

        This function samples pre-computed samples from its stored queues.
        These pre-computed samples are generated using BIER in the function
        train_models_and_precompute. Additionally, if a given point has changed
        neighbors or runs out of samples, it is retrained here to refill its
        precomputed samples before returning an interpolated_replay sample to
        train the agent with.

        NOTE: Because this replay buffer class leverages pre-computed samples,
        this replay method overrides the default replay function used in
        the InterpolatedReplayBuffer.

        Returns:
            batch (MultiAgentBatch): Returns a batch for the number of agents
                in the environment (note this is just one for non-MARL tasks.
        """
        if self._fake_batch:
            fake_batch = SampleBatch(self._fake_batch)
            return MultiAgentBatch({
                DEFAULT_POLICY_ID: fake_batch
            }, fake_batch.count)

        if self.num_added < self.replay_starts:
            return None

        # Initialize, if buffer has not been already
        if not self.initialized:
            print("INITIALIZING...")
            self.run_init()

        with self.replay_timer:

            # Use to set batches for all policies
            policy_batches = {}

            # Iterate through policies if MARL
            for policy_id, replay_buffer in self.replay_buffers.items():

                # Sample and query and get indices - NOTE: increments step count
                self.sample_and_query(sample_points_only=True)

                # Only need to check for changed neighbors if a sample is added
                if self.check_update_neighbors:

                    # Need to train the most recently-added point from scratch
                    self.train_models_and_precompute(
                        np.array(self.newest_idx), batch_size=1,
                        new_point=True, train=True)

                    # Retrain nonzero entries (idx where neighborhood changed)
                    if np.any(self.replace_mask):

                        # Find nonzero indices (already unique)
                        neighbor_retrain_idx = self.replace_mask.nonzero()[0]

                        # Loop to complete training - avoids flooding GPU
                        total_n_to_train = neighbor_retrain_idx.size  # Total
                        train_idx = 0  # Current idx
                        i = 0  # Current (integer) iteration of retraining

                        # Retrain in batches
                        while train_idx < total_n_to_train:

                            # Number to train in batch
                            idx_start = train_idx
                            idx_end = min(
                                self.queue_train_size + idx_start,
                                total_n_to_train
                            )

                            # Take next subset of neighbor indices to train
                            neighbor_idx_batch = neighbor_retrain_idx[idx_start: idx_end]

                            # Compute how many more points can be retrained
                            remaining_idx = max(
                                self.queue_train_size - neighbor_idx_batch.size, 0)

                            # Print number changed
                            if self.verbose:
                                print("STEP {}, REPEAT {}, "
                                      "NUMBER OF RETRAINS: {}".format(
                                    self.step_count, i, neighbor_idx_batch.size))

                            # Find k-largest counters, and retrain
                            if remaining_idx > 0:

                                # Number of additional idx available to train
                                k = remaining_idx

                                # Find k-largest indices (least samples left)
                                least_samples_idx = np.argpartition(
                                    self.counter[:self.total_sample_count], -k)[-k:]

                                # Take union of retrain and least sample indices
                                all_idx = np.union1d(
                                    neighbor_idx_batch,
                                    least_samples_idx).astype(np.int32)

                            # Just set neighbor_idx_batch to all_idx
                            else:
                                all_idx = neighbor_idx_batch.astype(np.int32)

                            # Retrain models where neighbors change
                            self.train_models_and_precompute(
                                all_idx, batch_size=all_idx.size,
                                new_point=False, train=True)

                            # Increment counter and idx for logging
                            i += 1
                            train_idx += idx_end - idx_start

                        # Reset neighbor replacement mask once finished
                        self.replace_mask = np.zeros(self.replace_mask.shape)

                        # Reset counters for idx any index where retrain occurs
                        self.counter[all_idx] = 0

                    # Reset neighbor checking condition
                    self.check_update_neighbors = False

                # Keep track of duplicates --> ensures unique training samples
                # Get unique idx and bincounts for indexing into samples
                unique_idx, bincounts = np.unique(
                    self.sample_indices, return_counts=True)

                # Placeholders - will be incremented
                repeat_tracker = np.zeros(
                    self.sample_indices.shape).astype(np.int32)
                repeats = {idx: 0 for idx in unique_idx}

                # Add repeats for incremental indexing into repeat samples
                for i, idx in enumerate(self.sample_indices):
                    repeat_tracker[i] = repeats[idx]
                    repeats[idx] += 1  # Increment to lookup next prediction

                # Compute counters for indexing into stored batch tensor
                counter_idx = self.counter[self.sample_indices] + repeat_tracker

                # Lookup counts at sample points + check if any out of samples
                # (Takes repeats into account as well)
                out_of_samples = counter_idx >= self.queue_size

                # Check if any retraining is required
                if np.any(out_of_samples):

                    if self.verbose:
                        print("STEP {}, RE-PREDICTING".format(self.step_count))

                    # Points out of samples (takes repeats into account)
                    idx_train = np.unique(self.sample_indices[out_of_samples])

                    # Compute how many more points can be retrained
                    remaining_idx = max(
                        self.queue_train_size - idx_train.size, 0)

                    # Find k-largest counters, and retrain
                    if remaining_idx > 0:

                        # Number of additional idx available to train
                        k = remaining_idx

                        # Find k-largest indices (least samples left)
                        # TODO(rms): Some samples may be double-counted
                        other_idx = np.argpartition(
                            self.counter[:self.total_sample_count], -k)[-k:]

                        # Take union of retrain and least sample indices
                        idx_train = np.union1d(
                            idx_train, other_idx).astype(np.int32)

                    # Perform inference only - no hyperparameter opt. needed
                    self.train_models_and_precompute(
                        idx_train, batch_size=idx_train.size,
                        new_point=False, train=False)

                    # Reset counters
                    self.counter[idx_train] = 0

                # Finally, get indices for samples
                sample_idx = self.counter[self.sample_indices] + repeat_tracker

                # Check if "too many" repeats --> circularly roll out samples
                idx_too_many_repeats = sample_idx >= self.queue_size
                if np.any(idx_too_many_repeats):
                    sample_idx[idx_too_many_repeats] = \
                        sample_idx[idx_too_many_repeats] % self.queue_size
                    print("WARNING: Too many repeated samples ({}) requested. "
                          "Can only accommodate up to {} sample repeats".format(
                            np.sum(idx_too_many_repeats), self.queue_size))

                # Now perform multidimensional np.array slicing for transitions
                obs_interpolated = self.queue_obs[
                    self.sample_indices, sample_idx, ...]  # Obs slice
                actions_interpolated = self.queue_actions[
                    self.sample_indices, sample_idx, ...]  # Action slice
                rewards_interpolated = self.queue_rewards[
                    self.sample_indices, sample_idx, ...]  # Reward slice
                new_obs_interpolated = self.queue_new_obs[
                    self.sample_indices, sample_idx, ...]  # New/delta obs slice

                # Extract linear interpolation coefficient
                b = self.queue_B[
                    self.sample_indices, sample_idx, ...]  # Interp coeff. slice

                # Extract likelihood weights + priorities, if used
                robust_weights = None
                if self.weighted_updates:

                    # Get unnormalized weights from sampling
                    unnormalized_weights = self.queue_likelihood_weights[
                        self.sample_indices, sample_idx, ...]  # llweights slice

                    # (vii) Normalize weights such that they have mean 1
                    normalizing_factor = self.replay_batch_size / np.sum(unnormalized_weights)
                    robust_weights = unnormalized_weights * normalizing_factor
                    """
                    median = np.median(unnormalized_weights)
                    clipped_weights = np.clip(unnormalized_weights,
                                              0.0, self.wll_max * median)

                    # Compute normalizing factor, scale, clip to [0, self.wll_max]
                    normalizing_factor = \
                        self.replay_batch_size / np.sum(clipped_weights)
                    robust_weights = clipped_weights * normalizing_factor

                    # Lastly, clip one more time to ensure we have no outliers
                    robust_weights = np.clip(robust_weights, 0, self.wll_max)
                    """
                    # Log weights to tensorboard writer
                    if self.step_count % (parameters.LOG_INTERVAL * self.replay_ratio) == 0:
                        self.writer.add_histogram(
                            'likelihood_weights', robust_weights,
                            self.step_count)

                        # Log the ratio of min: max
                        min_weight = np.min(robust_weights)
                        max_weight = np.max(robust_weights)
                        min_max_ratio = max_weight / min_weight
                        labels = ["Min Weight", "Max Weight", "Max:Min Ratio"]
                        values = [min_weight, max_weight, min_max_ratio]
                        for l, v in zip(labels, values):
                            self.writer.add_scalar(
                                "Likelihood Weights/{}".format(l),
                                v, global_step=self.step_count)

                # Extract neighbors, if using interpolated_replay priority updates
                neighbor_idx = np.squeeze(
                    self.queue_neighbor_idx[self.sample_indices, sample_idx])

                # If using interp priority update, need current prio
                if self.interp_prio_update:
                    sample_priorities = self.replay_buffers[self.policy_id].priorities[self.sample_indices]
                    neighbor_priorities = self.replay_buffers[self.policy_id].priorities[neighbor_idx]

                # Otherwise, can just set to None
                else:
                    sample_priorities, neighbor_priorities = None, None

                # Create interpolated_replay batch using precomputed samples
                interpolated_batch = self.sample_batch_wrapper_queue(
                    obs_interpolated, actions_interpolated,
                    rewards_interpolated, new_obs_interpolated,
                    self.sample_indices, self.prioritized_replay,
                    sample_weights=robust_weights,
                    interp_prio_update=self.interp_prio_update,
                    neighbor_indices=neighbor_idx, b=b,
                    sample_priorities=sample_priorities,
                    neighbor_priorities=neighbor_priorities)

                # Log interpolated_replay batch metrics to tensorboard
                if self.log_tb and self.step_count % \
                        (parameters.LOG_INTERVAL * self.replay_ratio) == 0:
                    self.compute_batch_metrics(interpolated_batch)

                # Now add interpolated_batch to policy_batches
                policy_batches[policy_id] = interpolated_batch

                # Visualize interpolated_replay batch
                if self.debug_plot and \
                        self.step_count % parameters.LOG_INTERVAL == 0:
                    pca_heatmap(interpolated_batch, self.dataset,
                                self.sample_indices, neighbor_idx, env=self.env)
                    pca_1D(interpolated_batch, self.dataset, self.sample_indices,
                           neighbor_idx, env=self.env)

                    # Tile b for when we perform PCA
                    B = np.tile(b, (self.d_r + self.d_s, 1)).T
                    if self.use_delta:  # Compute delta_obs and create y-axis
                        # Compute delta obs
                        delta_obs_interp = new_obs_interpolated - obs_interpolated
                        # Construct y-axis
                        plot_y = np.hstack((
                            np.expand_dims(rewards_interpolated, 1),
                            delta_obs_interp))

                    else:  # Create y-axis
                        # Use new_obs only
                        plot_y = np.hstack((
                            np.expand_dims(rewards_interpolated, 1),
                            new_obs_interpolated))

                    # Plot PCA comparing linear to GPR interpolation
                    plot_compare_linear_gp(
                        self.Y, self.sample_indices, neighbor_idx, plot_y,
                        idx=0, b=B, env=self.env, use_pca=False)

                # Increment counters for sampled points by number of repeats
                self.counter[unique_idx] = np.add(
                    self.counter[unique_idx], bincounts)

            # Perform holdout evaluation to evaluate interpolation
            time_for_holdout = self.step_count % self.steps_between_holdout == 0
            if self.perform_holdout_eval and time_for_holdout:
                self.perform_holdout_evaluation()  # Perform holdout evaluation

            # Compute GPR-specific metrics
            if self.step_count % (parameters.LOG_INTERVAL * self.replay_ratio) == 0:
                # Tile b for comparing linear to GPR
                B = np.tile(b, (self.d_r + self.d_s, 1)).T

                # Expand rewards
                expanded_rewards = rewards_interpolated.reshape((-1, 1))

                # Check whether predicting new obs or delta obs
                if self.use_delta:
                    gp_pred = np.hstack(
                        (expanded_rewards,
                         new_obs_interpolated - obs_interpolated))
                else:
                    gp_pred = np.hstack(
                        (expanded_rewards, new_obs_interpolated))

                # Compare GPR to linear
                self.compute_delta_linear_gp(self.Y, self.sample_indices,
                                             neighbor_idx, gp_pred, b=B)

            # Log metrics
            self.log_and_update_metrics()

            # Finally, return the batch
            return MultiAgentBatch(policy_batches, self.replay_batch_size)

    def train_models_and_precompute(self, training_idx, batch_size=None,
                                    new_point=False, train=False):
        """Function that performs training and precomputation of predicted
        points for a given set of GPR models.

        Namely, for a given set of training_idx and a batch_size, this model
        retrains a set of locally-defined GPR models.  Whether or not
        hyperparameter optimization is used to locally train each model is left
        as a design choice of the user.  Once these local GPR models are
        retrained, precomputed samples are queried at each point and stored for
        later sampling during replay operations.

        Parameters:
            training_idx (np.array): Array of points for which we should take
                models for.
            batch_size (int): The number of models we need to train for batching.
            new_point (bool): Whether the added point has just been added to the
                replay buffer. Defaults to False.
            train (bool): Whether to train the model. If True, and new_point is
                True, the model is trained "from scratch" for more epochs in
                order to properly fit the cluster. If new_point is False, the
                model is simply updated and trained for 1 epoch. Defaults to
                False, in which case the current hyperparams for a point are
                extracted.

        Modifies (through setting of slices for storing precomputed samples):
            self.queue_obs
            self.queue_actions
            self.queue_rewards
            self.queue_new_obs
            self.queue_B
            self.queue_likelihood_weights
            self.queue_neighbor_idx
        """
        # Select batch size if it has not been determined already
        if batch_size is None:
            batch_size = self.queue_train_size

        # Check if training_idx is one element
        if training_idx.size == 1:
            features = np.expand_dims(self.X_norm[training_idx, :], 0)
        else:
            features = self.X_norm[training_idx, :]

        # Start logging NN query time
        query_time_start = time.time()

        # Given a set of points to index, compute neighbors of these points
        # FAISS
        if self.use_faiss:
            neighbor_idx = self.faiss.query(features)

        # KD Tree
        elif self.use_kd_tree:
            neighbor_idx = self.kd_tree.query(features, k=self.kneighbors,
                return_distance=False, dualtree=True, sort_results=True)
        # KNN
        else:
            neighbor_idx = self.knn.kneighbors(features, return_distance=False)

        # Time neighbor/similarity querying
        query_time_end = time.time()
        self.total_query_time += query_time_end - query_time_start
        self.average_query_time = self.total_query_time / (self.step_count + 1)

        # Time interpolation
        time_start_interpolation = time.time()

        # Track time to create tensors
        time_start_gpr_tensor_creation = time.time()

        # Create training data
        Zs = torch.tensor(
            [self.X[k] for k in neighbor_idx], device=self.device)  # Features

        Ys = torch.tensor(
            [self.Y[k] for k in neighbor_idx], device=self.device)  # Targets

        # Cast to appropriate precision (fp32 or fp64)
        if self.fp32:  # Single-precision, i.e. fp32
            Zs = Zs.float()
            Ys = Ys.float()
        else:  # Double-precision, i.e. fp64
            Zs = Zs.double()
            Ys = Ys.double()

        # Log time for tensor creation
        time_end_gpr_tensor_creation = time.time()
        self.gpr_tensor_creation_time += time_end_gpr_tensor_creation - \
                                         time_start_gpr_tensor_creation

        # Normalize training features and targets
        if self.normalize:

            # Normalization timing
            time_start_gpr_normalization = time.time()

            # Map to standardized features and targets
            if self.standardize_gpr_x:
                Zs, Z_mean, Z_std, Ys, Y_mean, Y_std = \
                    standardize_features_standardize_targets(
                        Zs, Ys, keepdim_x=True, keepdim_y=True,
                        verbose=self.verbose, device=self.device)

            # Map to min-max normalized features and standardized targets
            else:
                Zs, Z_max, Z_min, Ys, Y_mean, Y_std = \
                    normalize_features_standardize_targets(
                        Zs, Ys, keepdim_x=True, keepdim_y=True,
                        verbose=self.verbose, device=self.device)

            # Log time taken for normalization
            time_end_gpr_normalization = time.time()
            self.gpr_normalization_time += time_end_gpr_normalization - \
                                           time_start_gpr_normalization

        # Track time to create model
        time_start_gpr_model_creation = time.time()

        # No existing hyperparameters to update - need to train from scratch
        if new_point:
            model_hyperparams = None

        # Extract current hyperparameters if updating existing points
        else:
            model_hyperparams = self.load_hyperparams(training_idx)

        # Specify whether to use global hyperparameters/not train further
        if train:  # Training on a new point
            global_hyperparams = False
        else:  # Performing inference only
            global_hyperparams = True

        # Specify epochs for training
        if new_point:  # Need to fit model from scratch, rather than fine-tune
            epochs = GP_EPOCHS[self.env]
        else:  # Only need to fine-tune the model
            epochs = 1

        # Train model via GPR hyperparameter optimization
        model, likelihood = gpr_train_batch(
            Zs, Ys, device=self.device, epochs=epochs,
            lr=GP_LR[self.env], thr=GP_THR,
            use_ard=self.use_ard, composite_kernel=self.composite_kernel,
            kernel=self.kernel, mean_type=self.mean_type,
            matern_nu=self.matern_nu,
            global_hyperparams=global_hyperparams,
            model_hyperparams=model_hyperparams,
            ds=self.d_s, use_priors=self.use_priors, use_lbfgs=self.use_lbfgs,
            est_lengthscales=self.est_lengthscales,
            lengthscale_constant=self.est_lengthscale_constant,
            update_hyperparams=train, fp64=not self.fp32)

        # Log time taken to create model
        time_end_gpr_model_creation = time.time()
        self.gpr_model_formation_time += time_end_gpr_model_creation - \
                                         time_start_gpr_model_creation

        # Check if stored hyperparams have been set
        if not self.hyperparameter_storage_set:
            self.init_hyperparam_storage(model)

        # If hyperparameters have been updated, extract and update them
        self.set_update_hyperparams(model, training_idx)

        # Place model in posterior mode, use vast predictive variances (LOVE)
        with torch.no_grad(), gpytorch.settings.fast_pred_var():

            # Time inference preprocessing
            time_start_gpr_inference_preprocessing = time.time()

            # Place model and likelihood into evaluation mode
            model.eval()
            likelihood.eval()

            # Create query points - Dimensions are (B, 1, XD)
            if batch_size == 1:
                X_s_np = np.expand_dims(self.X[training_idx], 0)  # Add batch d
            else:
                X_s_np = self.X[training_idx]

            X_s = torch.unsqueeze(
                torch.tensor(X_s_np, device=self.device), 1)

            # Determine if need to sample with replacement
            if self.furthest_neighbor is not None:
                replace = not (self.furthest_neighbor - 1 > self.queue_size)
            else:
                replace = not (self.kneighbors - 1 > self.queue_size)
            # Select neighbor(s) uniformly at random
            interp_indices = np.array(
                [np.random.choice(k[1:self.furthest_neighbor], self.queue_size,
                                  replace=replace) for k in neighbor_idx])

            # Add indices of neighbor points used in interpolation
            self.queue_neighbor_idx[training_idx, ...] = interp_indices

            # Slice dataset for interpolation points
            X_n = torch.tensor(self.X[interp_indices], device=self.device)

            # Now linearly interpolate to produce test point
            if self.use_smote or self.use_mixup:  # Use SMOTE or Mixup sampling

                # Determine how to sample linear interpolation coefficient
                if self.use_smote:  # Use SMOTE
                    b = torch.tensor(
                        np.random.random(size=(X_n.shape[:-1])),
                        device=self.device)
                elif self.use_mixup:  # Use Mixup sampling
                    b = torch.tensor(np.random.beta(
                        self.mixup_alpha, self.mixup_alpha,
                        size=X_n.shape[:-1]), device=self.device)

                # Log interpolation coefficient to Tensorboard
                if self.step_count % (parameters.LOG_INTERVAL * self.replay_ratio) == 0:
                    self.writer.add_histogram(
                        "Interpolation Coefficient", b, self.step_count)

                # Tile coefficient along feature dimensions
                B = torch.unsqueeze(b, -1).repeat((1, 1, X_s.shape[-1]))
                one_minus_B = torch.subtract(
                    torch.ones(B.shape, device=self.device), B)

                # Linearly interpolate (B, N, D) tensors
                X_q = torch.add(
                    torch.multiply(B, X_s),
                    torch.multiply(one_minus_B, X_n))

            else:  # Interpolate with arithmetic mean
                X_q = torch.add(X_s, X_n) / 2
                b = torch.ones(X_n.shape[:-1]) * 0.5

            # Normalize, if applicable
            if self.normalize:

                # Check whether to normalize or standardize features
                # Standardize features
                if self.standardize_gpr_x:
                    X_q_tensor = (X_q - Z_mean) / Z_std

                # Normalize features
                else:
                    X_q_tensor = (X_q - Z_min) / (Z_max - Z_min)

            else:  # Construct placeholder
                X_q_tensor = X_q

            # Cast to desired precision (fp32 or fp64)
            if self.fp32:
                X_q_tensor = X_q_tensor.float()
            else:
                X_q_tensor = X_q_tensor.double()

            # Now tile by the dimensionality of the targets (Y)
            X_q_tensor = X_q_tensor.repeat((self.d_r + self.d_s, 1, 1))

            # Log inference preprocessing time
            time_end_gpr_inference_preprocessing = time.time()
            self.gpr_inference_preprocessing_time += time_end_gpr_inference_preprocessing - \
                                                     time_start_gpr_inference_preprocessing
            # Time inference
            time_start_gpr_inference = time.time()

            # Perform inference using trained model
            observed_pred = likelihood(model(X_q_tensor))

            # Log inference time
            time_end_gpr_inference = time.time()
            self.gpr_inference_time += time_end_gpr_inference - \
                                       time_start_gpr_inference

            # Interpolated point is mean of prediction; weighted by variance
            samples = observed_pred.mean

            # Start gpr likelihood weights time
            time_start_gpr_likelihood_weights_time = time.time()

            if self.weighted_updates:  # Compute weights for updating losses

                # Take variance of each prediction
                variance = observed_pred.variance

                # Reformat the likelihoods to compute weights
                variance_block = format_preds_precomputed(
                    variance, batch_size, single_model=self.single_model)

                if self.normalize:
                    variance_block = variance_block * torch.square(Y_std)

                # (i) Compute likelihood
                likelihoods = torch.reciprocal(torch.sqrt(2 * np.pi * variance_block))

                # (ii) Take squashed log probabilities for numerical stability
                log_likelihoods = torch.log(likelihoods)

                # (iii) Take sum along samples to take product in log domain
                log_weights_summed = torch.sum(log_likelihoods, dim=-1)

                # (iv) Exponentiate to compute joint squashed likelihood
                weights = torch.exp(log_weights_summed)

                # (v) Take geometric mean of likelihood
                geometric_mean_weights = torch.pow(weights, 1 / (self.d_r + self.d_s))

                # (vi) Squash weights between [1/2, 1] for stability
                weights = torch.sigmoid(geometric_mean_weights).cpu().numpy()

                # Add non-normalized likelihood weights - (normalize when sampling)
                self.queue_likelihood_weights[training_idx, ...] = weights

                # Log weights to tensorboard writer
                if self.step_count % (parameters.LOG_INTERVAL * self.replay_ratio) == 0:
                    self.writer.add_histogram('unnormalized_likelihood_weights',
                                              weights, self.step_count)

            # Log timing metrics for likelihood weights
            time_end_gpr_likelihood_weights_time = time.time()
            self.gpr_likelihood_weights_time += time_end_gpr_likelihood_weights_time - \
                                                time_start_gpr_likelihood_weights_time

        # Reshape samples and normalize, if applicable
        out_y = format_preds_precomputed(
            samples, batch_size, single_model=self.single_model)

        # Unstandardize the targets to transform to input co-domain
        if self.normalize:
            out_y = (out_y * Y_std) + Y_mean
        else:
            out_y = out_y

        # Move inputs and predictions from GPU -> CPU simultaneously to amortize
        out_x = X_q.detach().cpu().numpy()  # Query point
        out_y = out_y.detach().cpu().numpy()  # Predicted point
        b = b.detach().cpu().numpy()  # Interpolation coefficient

        # Extract outputs from interpolated_replay x and predicted y
        # Recall: x = [obs  actions]^T,  y = [rewards  new_or_delta_obs]^T
        interpolated_obs = out_x[..., :self.d_s]
        interpolated_actions = out_x[..., self.d_s:]
        interpolated_rewards = np.squeeze(out_y[..., :self.d_r])
        interpolated_new_or_delta_obs = out_y[..., self.d_r:]

        # Observation slice
        self.queue_obs[training_idx, ...] = interpolated_obs  # Obs
        self.queue_actions[training_idx, ...] = interpolated_actions  # Actions
        self.queue_rewards[training_idx, ...] = interpolated_rewards  # Rewards

        # Set precomputed samples for new_obs according to new_obs or delta_obs
        if self.use_delta:  # obs + delta_obs
            self.queue_new_obs[training_idx, ...] = \
                interpolated_obs + interpolated_new_or_delta_obs

        else:  # new_obs
            self.queue_new_obs[training_idx, ...] = \
                interpolated_new_or_delta_obs

        # Store interpolation coefficients
        self.queue_B[training_idx, ...] = b

        # Delete model and likelihood to avoid GPU memory leak, and clear cache
        if self.use_cuda:
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

        # Delete tensors to avoid GPU memory leak
        del X_q, X_q_tensor, out_y, out_x, observed_pred, samples

        # Stop timing interpolation
        time_end_interpolation = time.time()
        self.interpolation_time += time_end_interpolation - \
                                   time_start_interpolation
        self.average_interpolation_time = self.interpolation_time / (self.step_count + 1)

        # Increment counter
        if int(self.step_count % self.replay_ratio) == 0:
            self.transitions_sampled += 1

        # Run a garbage collection run
        gc.collect()

    def init_hyperparam_storage(self, model):
        """Function to initialize the hyperparameter Tensors used for storing
        the hyperparameters of saved models."""
        # Get model hyperparameters
        params, names = self._extract_hyperparams(model)

        # Initialize formatted parameters
        param_sizes = {}

        # Format the hyperparameters
        for i, (name, param) in enumerate(zip(names, params)):

            # Reshape hyperparameters
            batch_size = min(self.queue_train_size, self.total_sample_count)
            reshaped_params = gputils.format_preds(
                param, batch_size, single_model=self.single_model)

            # Modify formatting for lengthscales and covariance
            if "lengthscale" in name:
                reshaped_params = torch.unsqueeze(reshaped_params, -2)
            elif "noise" in name:
                reshaped_params = torch.unsqueeze(reshaped_params, -1)

            # Create storage for array
            param_sizes[name] = (self.buffer_size, ) + reshaped_params.shape[1:]

        if self.composite_kernel:  # Separate lengthscales for states/actions

            # Parameters used in accordance with model constraints
            self.q_lengthscale_state = torch.zeros(
                param_sizes["state_base_kernel.lengthscale"])
            self.q_lengthscale_action = torch.zeros(
                param_sizes["action_base_kernel.lengthscale"])

            # Raw parameters learned via -mll minimization
            self.q_raw_lengthscale_state = torch.zeros(
                param_sizes["state_base_kernel.raw_lengthscale"])
            self.q_raw_lengthscale_action = torch.zeros(
                param_sizes["action_base_kernel.raw_lengthscale"])

        else:  # Single lengthscales for states/actions (concatenated)
            self.q_lengthscale = torch.zeros(
                param_sizes["covar_module.base_kernel.lengthscale"])
            self.q_raw_lengthscale = torch.zeros(
                param_sizes["covar_module.base_kernel.raw_lengthscale"])

        # Extract kernel factorization-independent hyperparameters
        self.q_covar_noise = torch.zeros(
            param_sizes["likelihood.noise_covar.noise"])
        self.q_outputscale = torch.zeros(
            param_sizes["covar_module.outputscale"])
        self.q_raw_noise = torch.zeros(
            param_sizes["likelihood.noise_covar.raw_noise"])

        # Determine what RQ parameters to use, if RQ kernel is used
        if self.kernel == "rq":
            if self.composite_kernel:
                self.q_alpha_state = torch.zeros(
                    param_sizes["covar_module.state_base_kernel.alpha"])
                self.q_alpha_action = torch.zeros(
                    param_sizes["covar_module.action_base_kernel.alpha"])
            else:
                self.q_alpha = torch.zeros(
                    param_sizes["covar_module.base_kernel.alpha"])

        # Determine what mean parameters to use based off of mean type
        if self.mean_type == "linear":
            self.q_mean_weights = torch.zeros(
                param_sizes["mean_module.weights"])
            self.q_mean_bias = torch.zeros(
                param_sizes["mean_module.bias"])

        elif self.mean_type == "constant":
            self.q_mean_constant = torch.zeros(
                param_sizes["mean_module.constant"])

        # Atttribute to set hyperparameter storage
        self.hyperparameter_storage_set = True

    def set_update_hyperparams(self, model, idx):
        """Function to set/update the hyperparameters stored in the replay
        buffer. The current hyperparameters of the model are extracted.

        Parameters:
            model (gpytorch.model.ExactGP): An instance of the ExactGP class.
                Can be batched, but this is not a requirement.
            idx (np.array): A NumPy array of the global replay buffer indices
                for storing each transition.

        Modifies (note this depends on the type of GPR model used):
            self.q_lengthscale_state <--- Composite kernel
            self.q_lengthscale_action <--- Composite kernel
            self.q_raw_lengtscale_state <--- Composite kernel
            slef.q_raw_lengthscale_action <--- Composite kernel
            self.q_covar_noise <--- Always modifies
            self.q_outputscale <--- Always modifies
            self.q_raw_noise <--- Always modifies
            self.q_alpha_state <--- Composite, Rational Quadratic (RQ) kernel
            self.q_alpha_action <--- Composite, Rational Quadratic (RQ) kernel
            self.q_alpha <--- Rational Quadratic (RQ) kernel
            self.q_mean_weights <--- Linear Mean Type
            self.q_mean_bias <--- Linear Mean Type
            self.q_mean_constant <--- Constant Mean Type
        """
        # Extract hyperparams and their names
        stacked_params = self._extract_hyperparams(model, as_dict=True)

        # Initialize output dict
        params = {}

        # Format params
        for i, (name, param) in enumerate(stacked_params.items()):
            reshaped_params = gputils.format_preds(
                param, idx.size, single_model=self.single_model)

            # Format params
            if "lengthscale" in name:
                reshaped_params = torch.unsqueeze(reshaped_params, -2)
            elif "noise" in name:
                reshaped_params = torch.unsqueeze(reshaped_params, -1)

            # Add entry to dictionary
            params[name] = reshaped_params

        # Conditionally modify along the batch axis for each hyperparameter set
        if self.composite_kernel:  # Separate lengthscales for states/actions

            # Parameters used in accordance with model constraints
            self.q_lengthscale_state[idx, ...] = \
                params["state_base_kernel.lengthscale"]
            self.q_lengthscale_action[idx, ...] = \
                params["action_base_kernel.lengthscale"]

            # Raw parameters learned via -mll minimization
            self.q_raw_lengthscale_state[idx, ...] = \
                params["state_base_kernel.raw_lengthscale"]
            self.q_raw_lengthscale_action[idx, ...] = \
                params["action_base_kernel.raw_lengthscale"]

        else:  # Single lengthscales for states/actions (concatenated)
            self.q_lengthscale[idx, ...] = \
                params["covar_module.base_kernel.lengthscale"]
            self.q_raw_lengthscale[idx, ...] = \
                params["covar_module.base_kernel.raw_lengthscale"]

        # Extract kernel factorization-independent hyperparameters
        self.q_covar_noise[idx, ...] = \
            params["likelihood.noise_covar.noise"]
        self.q_outputscale[idx, ...] = \
            params["covar_module.outputscale"]
        self.q_raw_noise[idx, ...] = \
            params["likelihood.noise_covar.raw_noise"]

        # Determine what RQ parameters to use, if RQ kernel is used
        if self.kernel == "rq":
            if self.composite_kernel:
                self.q_alpha_state[idx, ...] = \
                    params["covar_module.state_base_kernel.alpha"]
                self.q_alpha_action[idx, ...] = \
                    params["covar_module.action_base_kernel.alpha"]
            else:
                self.q_alpha[idx, ...] = \
                    params["covar_module.base_kernel.alpha"]

        # Determine what mean parameters to use based off of mean type
        if self.mean_type == "linear":
            self.q_mean_weights[idx, ...] = params["mean_module.weights"]
            self.q_mean_bias[idx, ...] = params["mean_module.bias"]

        elif self.mean_type == "constant":
            self.q_mean_constant[idx, ...] = params["mean_module.constant"]

    def load_hyperparams(self, idx):
        """Function to reload the trained hyperparameters of a model. Can be
        used for retraining and performing inference without the need to store
        expensive data.

        Parameters:
            idx (np.array): Array of indices for which the trained
                hyperparameters will be extracted.

        Returns:
            params (torch.Tensor): Array of hyperparameters corresponding
                to the model to be trained.
        """
        # Initialize dictionary to store queried hyperparameters
        params = {}

        # Conditionally modify along the batch axis for each hyperparameter set
        if self.composite_kernel:  # Separate lengthscales for states/actions

            # Parameters used in accordance with model constraints
            params["state_base_kernel.lengthscale"] = \
                self.q_lengthscale_state[idx, ...]
            params["action_base_kernel.lengthscale"] = \
                self.q_lengthscale_action[idx, ...]

            # Raw parameters learned via -mll minimization
            params["state_base_kernel.raw_lengthscale"] = \
                self.q_raw_lengthscale_state[idx, ...]
            params["action_base_kernel.raw_lengthscale"] = \
                self.q_raw_lengthscale_action[idx, ...]

        else:  # Single lengthscales for states/actions (concatenated)
            params["covar_module.base_kernel.lengthscale"] = \
                self.q_lengthscale[idx, ...]
            params["covar_module.base_kernel.raw_lengthscale"] = \
                self.q_raw_lengthscale[idx, ...]

        # Extract kernel factorization-independent hyperparameters
        params["likelihood.noise_covar.noise"] = \
            self.q_covar_noise[idx, ...]
        params["covar_module.outputscale"] = \
            self.q_outputscale[idx, ...]
        params["likelihood.noise_covar.raw_noise"] = \
            self.q_raw_noise[idx, ...]

        # Determine what RQ parameters to use, if RQ kernel is used
        if self.kernel == "rq":
            if self.composite_kernel:
                params["covar_module.state_base_kernel.alpha"] = \
                    self.q_alpha_state[idx, ...]
                params["covar_module.action_base_kernel.alpha"] = \
                    self.q_alpha_action[idx, ...]
            else:
                params["covar_module.base_kernel.alpha"] = \
                    self.q_alpha[idx, ...]

        # Determine what mean parameters to use based off of mean type
        if self.mean_type == "linear":
            params["mean_module.weights"] = self.q_mean_weights[idx, ...]
            params["mean_module.bias"] = self.q_mean_bias[idx, ...]

        elif self.mean_type == "constant":
            params["mean_module.constant"] = self.q_mean_constant[idx, ...]

        return params
