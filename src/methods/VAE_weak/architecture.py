

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from src.methods.VAE.architecture import VAE


class Encoder(nn.Module):

    def __init__(self, n_filters, input_shape, latent_dim, n_channel, **kwargs):
        super(Encoder, self).__init__()

        self.d = n_filters

        self.layer_count = 4

        # Encoder


        inputs = n_channel
        for i in range(self.layer_count):
            setattr(self, "conv%d" % (i + 1), nn.Conv2d(inputs, self.d, 4, stride= 2, padding=1))
            #print(inputs, self.d)
            #setattr(self, "conv%d_bn" % (i + 1), nn.BatchNorm2d(d * mul))
            inputs = self.d #* mul
            if (i+1)%2==0:
                self.d *=2

        self.linear_dim = 4 if input_shape == 64 else 8 if input_shape == 128 else 2
        self.linear = nn.Linear(in_features=inputs * self.linear_dim * self.linear_dim, out_features=latent_dim, bias=True)

        self.d_max = inputs



    def forward(self, x):


        for i in range(self.layer_count):
            x = F.leaky_relu(getattr(self, "conv%d" % (i + 1))(x))

        x = x.view(x.size(0), self.d_max * self.linear_dim * self.linear_dim)

        x = self.linear(x)
        return x


class Decoder(nn.Module):
    def __init__(self, n_filters, inputs, data_shape, latent_dim, n_channel, **kwargs):
        super(Decoder, self).__init__()


        self.inputs = inputs
        self.d_max = inputs
        self.layer_count = 4
        self.d = n_filters * (self.layer_count // 2)

        self.data_shape= np.array([-1] + [data_shape[-1]] + data_shape[:-1])

        # Decoder

        self.linear_dim = 4 if data_shape[0] == 64 else 8 if data_shape[0] == 128 else 2

        self.linear = nn.Linear(in_features=latent_dim, out_features=inputs * self.linear_dim * self.linear_dim, bias=True)


        for i in range(1, self.layer_count):
            setattr(self, "deconv%d" % (i + 1), nn.ConvTranspose2d(inputs, self.d, 4, 2, 1))
            inputs = self.d
            if (i + 1) % 2 == 0:
                self.d //= 2

        setattr(self, "deconv%d" % (self.layer_count + 1), nn.ConvTranspose2d(inputs, n_channel, 4, 2, 1))

    def forward(self, x):

        x = self.linear(x)

        x = x.view(x.size(0), self.d_max, self.linear_dim, self.linear_dim)
        for i in range(1, self.layer_count):
            x = F.leaky_relu((getattr(self, "deconv%d" % (i + 1))(x)))

        x = torch.tanh(getattr(self, "deconv%d" % (self.layer_count + 1))(x))

        return (x + 1.)/2.


class WEAKVAE(VAE):

    def __init__(self, warm_up_iterations,aggregator, n_filters, decoder_distribution,  beta, data_shape, latent_dim, n_channel, **kwargs):
        super(WEAKVAE, self).__init__(n_filters=n_filters, decoder_distribution=decoder_distribution,  beta=beta,data_shape=data_shape, latent_dim=latent_dim, n_channel= n_channel, **kwargs)

        # for the gaussian likelihood
        self.log_scale = nn.Parameter(torch.Tensor([0.0]))
        self.one = torch.Tensor([1.])

        self.clip_grad = 1.0 # clip value of gradient norm
        self.warm_up_iterations = warm_up_iterations

        # linear warm-up

        if self.warm_up_iterations >0:

            self.linear_betas = np.linspace(0., beta, num=warm_up_iterations, endpoint=True)
            self.beta = 0.
        else:
            self.beta = beta

        self.current_iteration = 0

        self.aggregating_func = aggregator  # "argmax" "labels"


    def update_beta(self):

        self.current_iteration += 1

        if self.warm_up_iterations <= 0:
            return

        if self.current_iteration >= self.warm_up_iterations:
            return

        self.beta = self.linear_betas[self.current_iteration]

    def compute_loss_couple(self, x1, x2, labels):


        x12_encoded = self.encoder.forward(torch.cat((x1, x2), dim=0))
        mu12, log_var12 = self.fc_mu(x12_encoded ), self.fc_var(x12_encoded )

        mu1, mu2 = torch.chunk(mu12, chunks = 2, dim=0)
        log_var1, log_var2 = torch.chunk(log_var12, chunks=2, dim=0)


        labels = torch.squeeze(F.one_hot(labels, list(mu1.size())[1]))


        kl_per_point = 0.5 * self.compute_kl(mu1, mu2, log_var1, log_var2) + 0.5 * self.compute_kl(mu2, mu1, log_var2, log_var1)

        new_mean = 0.5 * mu1 + 0.5 * mu2
        var_1 = log_var1.exp()
        var_2 = log_var2.exp()
        new_log_var = torch.log(0.5 * var_1 + 0.5 * var_2)



        # aggregate distributions with argmax
        mean_sample_1, log_var_sample_1 = self.aggregate(mu1, log_var1, new_mean, new_log_var, labels, kl_per_point)
        mean_sample_2, log_var_sample_2 = self.aggregate(mu2, log_var2, new_mean, new_log_var, labels, kl_per_point)


        # sample points from new distributions
        z1 = self.z_sample(mean_sample_1, log_var_sample_1)
        z2 = self.z_sample(mean_sample_2, log_var_sample_2)

        # reconstruct
        y_hat12 = self.decoder.forward(torch.cat((z1, z2), dim=0))
        y_hat1, y_hat2 = torch.chunk(y_hat12, chunks=2, dim=0)

        # reconstruction loss
        recon_loss1 = self.reconstruction(y_hat1, self.log_scale, x1)
        recon_loss2 = self.reconstruction(y_hat2, self.log_scale, x2)
        reconstruction_loss = (0.5 * recon_loss1 + 0.5 * recon_loss2).mean()

        # kl loss
        kl_loss_1 = self.kl_divergence( mean_sample_1, log_var_sample_1)
        kl_loss_2 = self.kl_divergence( mean_sample_2, log_var_sample_2)
        kl_loss = (0.5 * kl_loss_1 + 0.5 * kl_loss_2).mean()


        elbo = reconstruction_loss - self.beta * kl_loss

        loss = {"reconstruction": reconstruction_loss, "kl": kl_loss, "loss": -elbo,  "elbo": reconstruction_loss -  kl_loss} # -elbo

        return  loss, y_hat1, y_hat2



    def aggregate(self, z_mean, z_logvar, new_mean, new_log_var, labels, kl_per_point):

        if self.aggregating_func == "argmax":
            return self.aggregate_argmax( z_mean, z_logvar, new_mean, new_log_var, labels, kl_per_point)

        return self.aggregate_labels( z_mean, z_logvar, new_mean, new_log_var, labels, kl_per_point)


    def aggregate_argmax(self, z_mean, z_logvar, new_mean, new_log_var, labels, kl_per_point):
        """Argmax aggregation with adaptive k.

         The bottom k dimensions in terms of distance are not averaged. K is
         estimated adaptively by binning the distance into two bins of equal width.

         Args:
           z_mean: Mean of the encoder distribution for the original image.
           z_logvar: Logvar of the encoder distribution for the original image.
           new_mean: Average mean of the encoder distribution of the pair of images.
           new_log_var: Average logvar of the encoder distribution of the pair of
             images.
           labels: One-hot-encoding with the position of the dimension that should not
             be shared.
           kl_per_point: Distance between the two encoder distributions.

         Returns:
           Mean and logvariance for the new observation.
         """

        del labels

        one = self.one.repeat(kl_per_point.size()).to(self.device)
        mask = self.discretize_in_bins(kl_per_point).eq(one)

        z_mean_averaged = torch.where(mask, z_mean, new_mean)
        z_logvar_averaged = torch.where(mask, z_logvar, new_log_var)

        return z_mean_averaged, z_logvar_averaged


    def aggregate_labels(self, z_mean, z_logvar, new_mean, new_log_var, labels, kl_per_point):

        """Use labels to aggregate.

        Labels contains a one-hot encoding with a single 1 of a factor shared. We
        enforce which dimension of the latent code learn which factor (dimension 1
        learns factor 1) and we enforce that each factor of variation is encoded in a
        single dimension.

        Args:
          z_mean: Mean of the encoder distribution for the original image.
          z_logvar: Logvar of the encoder distribution for the original image.
          new_mean: Average mean of the encoder distribution of the pair of images.
          new_log_var: Average logvar of the encoder distribution of the pair of
            images.
          labels: One-hot-encoding with the position of the dimension that should not
            be shared.
          kl_per_point: Distance between the two encoder distributions (unused).

        Returns:
          Mean and logvariance for the new observation.
        """

        del kl_per_point

        max_labels = torch.unsqueeze(torch.amax(labels, dim=-1), -1)
        z_mean_averaged = torch.where(torch.eq(labels, max_labels ), z_mean, new_mean)
        z_logvar_averaged = torch.where(torch.eq(labels, max_labels ), z_logvar, new_log_var)

        return z_mean_averaged, z_logvar_averaged

    def update_weights(self):
        '''Compute gradients and update weights'''

        nn.utils.clip_grad_norm_(self.parameters(), self.clip_grad)
        # update model weights
        self._optimizer.step()


    def compute_kl(self, mean1, mean2, log_var1, log_var2):
        """Compute the Kullback-Leibler divergence between two Gaussian distributions."""
        # Calculate variances
        var1 = torch.exp(log_var1)
        var2 = torch.exp(log_var2)

        # Calculate the KL divergence
        kl = var1/var2 + torch.square(mean2-mean1)/var2 - 1. + log_var2 - log_var1
        return kl


    def discretize_in_bins(self, x):
      """Discretize a vector in two bins."""
      return self.histogram_fixed_width_bins(x, torch.min(x).item(), torch.max(x).item(), nbins=2)


    def histogram_fixed_width_bins(self, values, min, max, nbins):
        """
        Given the tensor values, this operation returns a rank 1 Tensor representing the indices of a histogram into which each element of values would be binned.
        The bins are equal width and determined by the arguments value_range and nbins.
        """

        value_range = [min, max]

        # Calculate the width of each bin
        bin_width = (value_range[1] - value_range[0]) / nbins

        # Compute the indices of bin placement for each value
        indices = ((values - value_range[0]) / bin_width).floor().clamp(0, nbins - 1).long()
        return indices


