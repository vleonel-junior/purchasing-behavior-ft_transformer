import os
import json
import numpy as np
import torch
import optuna
from train_funct import train, val, evaluate
from data.process_telecom_data import device, get_data
import zero
import rtdl
from num_embedding_factory import get_num_embedding
import logging

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Paramètres fixes ---
seeds = [0, 1, 2]
metrics_dir = "results/results_telecom/ftt_optuna/"
os.makedirs(metrics_dir, exist_ok=True)

def objective(trial):
    """Fonction objectif optimisée pour Optuna"""
    
    # 1. Hyperparamètres avec espaces de recherche étendus
    lr = trial.suggest_loguniform("lr", 1e-5, 1e-1)
    weight_decay = trial.suggest_loguniform("weight_decay", 1e-6, 1e-1)
    num_embedding_type = trial.suggest_categorical("num_embedding_type", ["L", "LR", "LR-LR", "P", "P-LR", "P-LR-LR"])
    n_heads = trial.suggest_categorical("n_heads", [4, 8, 16])
    d_embedding = trial.suggest_categorical("d_embedding", [16, 32, 64])
    n_layers = trial.suggest_int("n_layers", 1, 6)
    attention_dropout = trial.suggest_uniform("attention_dropout", 0.0, 0.3)
    ffn_dropout = trial.suggest_uniform("ffn_dropout", 0.0, 0.3)
    residual_dropout = trial.suggest_uniform("residual_dropout", 0.0, 0.2)
    batch_size = trial.suggest_categorical("batch_size", [32, 64, 128])
    
    # 2. Early stopping plus sophistiqué
    patience_epochs = 10
    min_delta = 1e-4
    
    try:
        aucs = []
        pr_aucs = []
        
        for seed_idx, seed in enumerate(seeds):
            logger.info(f"Trial {trial.number}, Seed {seed_idx+1}/{len(seeds)}")
            
            X, y, cat_cardinalities = get_data(seed)
            
            # Loaders avec batch_size variable
            train_loader = zero.data.IndexLoader(len(y['train']), batch_size, device=device)
            val_loader = zero.data.IndexLoader(len(y['val']), batch_size, device=device)
         
            num_embedding = get_num_embedding(
            embedding_type = num_embedding_type,
            X_train = X['train'][0],
            d_embedding=d_embedding,
            y_train = y['train'] if num_embedding_type in ("T", "T-L", "T-LR", "T-LR-LR") else None
    )
            
            model = rtdl.FTTransformer(
                n_num_features=X['train'].shape[1],
                cat_cardinalities=cat_cardinalities,
                d_token=d_embedding,
                n_heads=n_heads,
                n_layers=n_layers,
                attention_dropout=attention_dropout,
                ffn_dropout=ffn_dropout,
                residual_dropout=residual_dropout,
                d_out=1,
                last_layer_query_idx=[-1],
            )
            
            model.feature_tokenizer.num_tokenizer = num_embedding
            model.to(device)
            
            optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                optimizer, mode='min', patience=5, factor=0.5, verbose=False
            )
            loss_fn = torch.nn.BCELoss()
            
            best_val_loss = float('inf')
            best_metrics = None
            patience_counter = 0
            
            for epoch in range(100):
                loss_train = train(epoch, model, optimizer, X, y, train_loader, loss_fn)
                loss_val = val(epoch, model, X, y, val_loader, loss_fn)
                
                scheduler.step(loss_val)
                
                trial.report(loss_val, epoch)
                if trial.should_prune():
                    raise optuna.TrialPruned()
                
                if loss_val < best_val_loss - min_delta:
                    best_val_loss = loss_val
                    patience_counter = 0
                    metrics = evaluate(model, 'test', X, y, seed)
                    best_metrics = metrics
                else:
                    patience_counter += 1
                    if patience_counter >= patience_epochs:
                        logger.info(f"Early stopping at epoch {epoch}")
                        break
            
            if best_metrics is None:
                best_metrics = evaluate(model, 'test', X, y, seed)
            
            aucs.append(best_metrics[0])
            pr_aucs.append(best_metrics[1])
            
            del model, optimizer, scheduler
            torch.cuda.empty_cache() if torch.cuda.is_available() else None
        
        mean_auc = np.mean(aucs)
        std_auc = np.std(aucs)
        mean_pr_auc = np.mean(pr_aucs)
        
        trial.set_user_attr("detailed_results", {
            "hyperparams": {
                "lr": lr,
                "weight_decay": weight_decay,
                "num_embedding_type": num_embedding_type,
                "n_heads": n_heads,
                "d_embedding": d_embedding,
                "n_layers": n_layers,
                "attention_dropout": attention_dropout,
                "ffn_dropout": ffn_dropout,
                "residual_dropout": residual_dropout,
                "batch_size": batch_size,
            },
            "results": {
                "aucs_per_seed": aucs,
                "pr_aucs_per_seed": pr_aucs,
                "mean_auc": mean_auc,
                "std_auc": std_auc,
                "mean_pr_auc": mean_pr_auc,
                "seeds": seeds,
            }
        })
        
        return mean_auc
        
    except Exception as e:
        logger.error(f"Error in trial {trial.number}: {str(e)}")
        raise

if __name__ == "__main__":
    sampler = optuna.samplers.TPESampler(
        n_startup_trials=10,
        n_ei_candidates=24,
        seed=42
    )
    
    pruner = optuna.pruners.MedianPruner(
        n_startup_trials=5,
        n_warmup_steps=10,
        interval_steps=5
    )
    
    study = optuna.create_study(
        direction="maximize",
        study_name="ftt_optuna_enhanced",
        sampler=sampler,
        pruner=pruner
    )
    
    def save_callback(study, trial):
        if trial.number % 5 == 0:
            with open(os.path.join(metrics_dir, "intermediate_results.json"), "w") as f:
                results = []
                for t in study.trials:
                    if t.value is not None:
                        result = {"trial_number": t.number, "value": t.value}
                        result.update(t.params)
                        if "detailed_results" in t.user_attrs:
                            result.update(t.user_attrs["detailed_results"])
                        results.append(result)
                json.dump(results, f, indent=2)
    
    try:
        study.optimize(
            objective, 
            n_trials=50,
            callbacks=[save_callback],
            show_progress_bar=True
        )
    except KeyboardInterrupt:
        logger.info("Optimization interrupted by user")
    
    best_trial = study.best_trial
    
    with open(os.path.join(metrics_dir, "best_params.json"), "w") as f:
        json.dump(best_trial.params, f, indent=2)
    
    if "detailed_results" in best_trial.user_attrs:
        with open(os.path.join(metrics_dir, "best_detailed_results.json"), "w") as f:
            json.dump(best_trial.user_attrs["detailed_results"], f, indent=2)
    
    all_trials = []
    for t in study.trials:
        if t.value is not None:
            trial_data = {
                "trial_number": t.number,
                "value": t.value,
                "params": t.params,
                "state": t.state.name
            }
            if "detailed_results" in t.user_attrs:
                trial_data["detailed_results"] = t.user_attrs["detailed_results"]
            all_trials.append(trial_data)
    
    with open(os.path.join(metrics_dir, "all_trials_detailed.json"), "w") as f:
        json.dump(all_trials, f, indent=2)
    
    logger.info(f"Optimization completed!")
    logger.info(f"Best trial: {best_trial.number}")
    logger.info(f"Best mean AUC: {best_trial.value:.4f}")
    logger.info(f"Best params: {best_trial.params}")
    
    if len(study.trials) > 10:
        importance = optuna.importance.get_param_importances(study)
        logger.info("Parameter importance:")
        for param, imp in importance.items():
            logger.info(f"  {param}: {imp:.4f}")
        
        with open(os.path.join(metrics_dir, "param_importance.json"), "w") as f:
            json.dump(importance, f, indent=2)