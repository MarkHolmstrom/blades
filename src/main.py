import importlib
import inspect
import os
import sys
from time import time

import numpy as np
import torch
from torch.nn.modules.loss import CrossEntropyLoss

from args import parse_arguments

current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)
from simulators.server import TorchServer
from settings.cifar10 import cifar10
from utils import top1_accuracy, initialize_logger
from simulators.datamanager import DataManager
from simulators.simulator import (ParallelTrainer, DistributedEvaluator)

options = parse_arguments()

agg_path = importlib.import_module('aggregators.%s' % options.agg)
agg_scheme = getattr(agg_path, options.agg.capitalize())


def main(args):
    initialize_logger(options.log_dir)
    device = torch.device("cuda" if args.use_cuda else "cpu")
    
    kwargs = {"pin_memory": True} if args.use_cuda else {}
    
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    
    opt = importlib.import_module(options.model_path)
    Model = getattr(opt, "Net")
    model = Model().to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=options.lr)
    loss_func = CrossEntropyLoss().to(device)
    
    metrics = {"top1": top1_accuracy}
    
    server_opt = torch.optim.SGD(model.parameters(), lr=options.lr)
    server = TorchServer(server_opt, model=model)
    data_mgr = DataManager(data_path=options.data_path, train_bs=options.batch_size, test_bs=options.test_batch_size)
    trainer = ParallelTrainer(
        server=server,
        aggregator=agg_scheme(options),
        data_manager=data_mgr,
        max_batches_per_epoch=options.local_round,
        log_interval=args.log_interval,
        metrics=metrics,
        use_cuda=args.use_cuda,
        debug=False,
        num_trainers=args.num_trainers,
        gpu_per_actor=args.gpu_per_actor,
        num_actors=args.num_actors,
        use_actor=args.use_actor
    )
    
    test_loader = cifar10(
        data_dir=options.data_dir,
        data_path=options.data_path,
        train=False,
        download=True,
        batch_size=options.test_batch_size,
        shuffle=False,
        worker_rank=None,
        **kwargs,
    )
    
    scheduler = torch.optim.lr_scheduler.MultiStepLR(
        server_opt, milestones=[75, 100], gamma=0.5
    )
    
    evaluator = DistributedEvaluator(
        model=model,
        data_loader=test_loader,
        loss_func=loss_func,
        device=device,
        metrics=metrics,
        use_cuda=args.use_cuda,
        debug=False,
    )
    
    trainer.setup_clients(options.data_path, model, loss_func, device, optimizer)
    if args.use_actor:
        # trainer.parallel_call(lambda worker: worker.detach_model.remote())
        trainer.parallel_call(lambda worker: worker.detach_model())
    else:
        trainer.parallel_call(lambda worker: worker.detach_model())
    
    time_start = time()
    for round in range(1, options.round + 1):
        if args.fedavg:
            if args.use_actor:
                trainer.train_fedavg_actor(round, options.local_round)
            else:
                trainer.train_fedavg(round, options.local_round)
        else:
            trainer.train(round)
        if args.use_actor:
            trainer.test_actor(global_round=round, batch_size=options.test_batch_size)
        # evaluator.evaluate(epoch)
        scheduler.step()
        print(f"E={round}; Learning rate = {scheduler.get_last_lr()[0]:}; Time cost = {time() - time_start}")
    # evaluator.evaluate(epoch)


if __name__ == "__main__":
    import ray
    
    if not ray.is_initialized():
        # ray.init(local_mode=True, include_dashboard=True, num_gpus=options.num_gpus)
        ray.init(include_dashboard=True, num_gpus=options.num_gpus)
    main(options)
