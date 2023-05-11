import lightning.pytorch as pl
import torch.optim
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
import inspect

from anyGPT.config.settings import AnyGPTSettings
from anyGPT.models.architectures import AnyGPT
from anyGPT.models.modules import LayerNorm


class AnyGPTLit(pl.LightningModule):
    def __init__(self, settings: AnyGPTSettings):
        super().__init__()
        self.settings = settings
        self.model = AnyGPT(self.settings.model_config)
        self.save_hyperparameters()

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def training_step(self, batch, batch_index):
        x, y = batch
        logits, loss = self.model(x, y)
        self.log("train/loss", loss)
        return loss

    def validation_step(self, batch, batch_index):
        x, y = batch
        logits, loss = self.model(x, y)
        self.log("val/loss", loss)
        return loss

    def configure_optimizers(self):
        decay = set()
        no_decay = set()
        modules_to_decay = torch.nn.Linear
        modules_not_to_decay = (torch.nn.LayerNorm, LayerNorm, torch.nn.Embedding)
        learning_rate = self.settings.training_config.learning_rate
        betas = (
            self.settings.training_config.beta1,
            self.settings.training_config.beta2,
        )
        if self.settings.training_config.decay_lr:
            for mn, m in self.named_modules():
                for pn, p in m.named_parameters():
                    fpn = "%s.%s" % (mn, pn) if mn else pn  # full param name
                    # random note: because named_modules and named_parameters are recursive
                    # we will see the same tensors p many many times. but doing it this way
                    # allows us to know which parent module any tensor p belongs to...
                    if pn.endswith("bias"):
                        # all biases will not be decayed
                        no_decay.add(fpn)
                    elif pn.endswith("weight") and isinstance(m, modules_to_decay):
                        # weights of whitelist modules will be weight decayed
                        decay.add(fpn)
                    elif pn.endswith("weight") and isinstance(m, modules_not_to_decay):
                        # weights of blacklist modules will NOT be weight decayed
                        no_decay.add(fpn)
            decay.remove("model.lm_head.weight")
            param_dict = {pn: p for pn, p in self.named_parameters()}
            inter_params = decay & no_decay
            union_params = decay | no_decay
            assert (
                len(inter_params) == 0
            ), "parameters %s made it into both decay/no_decay sets!" % (
                str(inter_params),
            )
            assert (
                len(param_dict.keys() - union_params) == 0
            ), "parameters %s were not separated into either decay/no_decay set!" % (
                str(param_dict.keys() - union_params),
            )
            optim_groups = [
                {
                    "params": [param_dict[pn] for pn in sorted(list(decay))],
                    "initial_lr": self.settings.training_config.learning_rate,
                    "weight_decay": self.settings.training_config.weight_decay,
                },
                {
                    "params": [param_dict[pn] for pn in sorted(list(no_decay))],
                    "initial_lr": self.settings.training_config.learning_rate,
                    "weight_decay": 0.0,
                },
            ]
        else:
            optim_groups = self.parameters()

        fused_available = "fused" in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and self.device == "cuda"
        extra_args = dict(fused=True) if use_fused else dict()
        optimizer = torch.optim.AdamW(
            optim_groups, lr=learning_rate, betas=betas, **extra_args
        )
        if self.settings.training_config.decay_lr:
            scheduler = CosineAnnealingWarmRestarts(
                optimizer,
                T_0=self.settings.training_config.warmup_iters,
                eta_min=self.settings.training_config.min_lr,
                last_epoch=self.settings.training_config.max_steps,
            )
            return [optimizer], [scheduler]
        else:
            return optimizer
