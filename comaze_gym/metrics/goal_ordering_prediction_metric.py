from typing import List, Dict 

import torch
import torch.nn as nn 
import torch.nn.functional as F

import numpy as np 
import copy

from comaze_gym.metrics.hiddenstate_policy import HiddenStatePolicy


class GoalOrderingPredictionMetric(object):
    def __init__(self, hiddenstate_policy:HiddenStatePolicy, label_dim:int=5*4):
        """
        
        :param hiddenstate_policy:
            (Reference to an) HiddenStatePolicy: Expects a (wrapped) torch.nn.Module that outputs 
            the inner hidden state representation of the agent.

        """
        self.label_dim = label_dim+4*4
        self.hiddenstate_policy = hiddenstate_policy
        self.hidden_state_dim = self.hiddenstate_policy.get_hidden_state_dim()
        self.prediction_net = [
            nn.Linear(self.hidden_state_dim, 512),
            nn.ReLU(),
            #nn.Dropout(p=0.5),
            nn.Linear(512,self.label_dim)
        ]
        self.prediction_net = nn.Sequential(*self.prediction_net)
        print(self.prediction_net)

    def compute_goal_ordering_prediction_loss(self, x:List[List[object]], y:List[torch.Tensor], yp:List[torch.Tensor], mask:List[List[object]]=None, biasing:bool=False) -> torch.Tensor:
        """
        WARNING: this function resets the :attr hiddenstate_policy:! 
        Beware of potentially erasing agent's current's internal states

        :param x: 
            List[List[object]] containing, for each actor, at each time step t an object
            representing the observation of the current agent.
            e.g.: the object can be a kwargs argument containing
            expected argument to the :attr hiddenstate_policy:.
        
        :param x: 
            List[torch.Tensor] containing, for each actor, an object
            representing the labels for the prediction of reached goals ordering.
            Shape: 4

        :param mask:
            List[List[object]] containing, for each actor, at each time step t an object
            with batch_size dimensions and whose values are either
            1 or 0. For all actor b, mask[b]==1 if and only if
            the experience in x[t] is valid (e.g. episode not ended).
        """
        batch_size = len(x)
        
        nbr_actors = self.hiddenstate_policy.get_nbr_actor()

        if biasing:
            hiddenstate_policy = self.hiddenstate_policy
            self.hiddenstate_policy.save_inner_state()
        else:
            hiddenstate_policy = self.hiddenstate_policy.clone()
        
        L_gop = torch.zeros(batch_size)
        L_rp = torch.zeros(batch_size)
        per_actor_per_t_per_goal_acc = [[] for _ in range(batch_size)]
        per_actor_per_t_per_rule_acc = [[] for _ in range(batch_size)]
        
        per_actor_acc_distr_q1 = torch.zeros(batch_size)
        per_actor_gop_accuracy = torch.zeros(batch_size)

        per_actor_rp_acc_distr_q1 = torch.zeros(batch_size)
        per_actor_rp_accuracy = torch.zeros(batch_size)

        for actor_id in range(batch_size):
            hiddenstate_policy.reset(1)
            labels = y[actor_id].long()
            # 1x4
            rules_labels = yp[actor_id].long()
            # 1x4

            T = len(x[actor_id])
            if mask is None:
                eff_mask = torch.ones((batch_size, T))
            else:
                eff_mask = mask 

            for t in range(T):
                m = eff_mask[actor_id][t] 
                
                if biasing:
                    hs_t = hiddenstate_policy(x[actor_id][t])
                    # 1 x hidden_state_dim
                else:
                    with torch.no_grad():
                        hs_t = hiddenstate_policy(x[actor_id][t]).detach()
                    # 1 x hidden_state_dim
                
                pred = self.prediction_net(hs_t.reshape(1,-1))
                pred_ordering = pred[:,:4*5].reshape((1,4,5))
                pred_rules = pred[:,4*5:].reshape((1,4,4))

                log_pred_distr = torch.log_softmax(pred_ordering, dim=-1)
                # 1 x 4 x 5
                log_rules_distr = torch.log_softmax(pred_rules, dim=-1)
                # 1 x 4 x 4
                
                m = m.to(hs_t.device)
                if labels.device != pred.device: labels = labels.to(pred.device)    
                if rules_labels.device != pred.device: rules_labels = rules_labels.to(pred.device)    
                
                ###                
                pred_goals = log_pred_distr.argmax(dim=-1)
                # 1x4
                per_goal_acc_t = (pred_goals==labels).float()
                # 1x4
                per_actor_per_t_per_goal_acc[actor_id].append(per_goal_acc_t)
                ###
                pred_goals_rules = log_rules_distr.argmax(dim=-1)
                # 1x4
                per_rule_acc_t = (pred_goals_rules==rules_labels).float()
                # 1x4
                per_actor_per_t_per_rule_acc[actor_id].append(per_rule_acc_t)
                ###

                L_gop_t = 0
                for l_id in range(4):
                    L_gop_t = L_gop_t + F.nll_loss(
                        input=log_pred_distr[:,l_id], # 1x5
                        target=labels[:,l_id].detach(), # 1
                        reduction='none'
                    ).sum()#.sum(dim=-1)
                    # 1 
                
                L_rp_t = 0
                for l_id in range(4):
                    L_rp_t = L_rp_t + F.nll_loss(
                        input=log_rules_distr[:,l_id], # 1x5
                        target=rules_labels[:,l_id].detach(), # 1
                        reduction='none'
                    ).sum()#.sum(dim=-1)
                    # 1 
                
                if L_gop.device != L_gop_t.device:    L_gop = L_gop.to(L_gop_t.device)
                L_gop[actor_id:actor_id+1] += m*L_gop_t.reshape(-1)
                # batch_size

                if L_rp.device != L_rp_t.device:    L_rp = L_rp.to(L_rp_t.device)
                L_rp[actor_id:actor_id+1] += m*L_rp_t.reshape(-1)
                # batch_size

            per_actor_per_t_per_goal_acc[actor_id] = torch.cat(per_actor_per_t_per_goal_acc[actor_id], dim=0)
            # timesteps x nbr_goal
            per_actor_per_t_per_rule_acc[actor_id] = torch.cat(per_actor_per_t_per_rule_acc[actor_id], dim=0)
            # timesteps x nbr_goal
        
            """
            For each actor: 
            1) extract indices where prediction is correct
            2) compute stats on this distribution of indices, e.g. first quartile.
            """
            correct_pred_indices = torch.nonzero((per_actor_per_t_per_goal_acc[actor_id].sum(dim=-1)==4).float())
            # (min:0, max:timesteps x 1)
            if correct_pred_indices.shape[0]>=1:
                median_value = np.nanpercentile(
                    correct_pred_indices,
                    q=50,
                    axis=None,
                    interpolation="nearest"
                )
                q1_value = np.nanpercentile(
                    correct_pred_indices,
                    q=25,
                    axis=None,
                    interpolation="lower"
                )
                q3_value = np.nanpercentile(
                    correct_pred_indices,
                    q=75,
                    axis=None,
                    interpolation="higher"
                )
                iqr = q3_value-q1_value
            else:
                median_value = 100
                q1_value = 100
                q3_value = 100
                iqr = 0

            per_actor_acc_distr_q1[actor_id] = float(q1_value)
            per_actor_gop_accuracy[actor_id] = correct_pred_indices.shape[0]/T*100.0

            ###

            correct_pred_indices = torch.nonzero((per_actor_per_t_per_rule_acc[actor_id].sum(dim=-1)==4).float())
            # (min:0, max:timesteps x 1)
            if correct_pred_indices.shape[0]>=1:
                median_value = np.nanpercentile(
                    correct_pred_indices,
                    q=50,
                    axis=None,
                    interpolation="nearest"
                )
                q1_value = np.nanpercentile(
                    correct_pred_indices,
                    q=25,
                    axis=None,
                    interpolation="lower"
                )
                q3_value = np.nanpercentile(
                    correct_pred_indices,
                    q=75,
                    axis=None,
                    interpolation="higher"
                )
                iqr = q3_value-q1_value
            else:
                median_value = 100
                q1_value = 100
                q3_value = 100
                iqr = 0

            per_actor_rp_acc_distr_q1[actor_id] = float(q1_value)
            per_actor_rp_accuracy[actor_id] = correct_pred_indices.shape[0]/T*100.0

            ###
            
        if biasing:
            self.hiddenstate_policy.reset(nbr_actors, training=True)
            self.hiddenstate_policy.restore_inner_state()

        output_dict = {
            'l_gop':L_gop, 
            'per_actor_gop_accuracy':per_actor_gop_accuracy, 
            'per_actor_acc_distr_q1':per_actor_acc_distr_q1,
            #

            'l_rp':L_rp, 
            'per_actor_rp_accuracy':per_actor_rp_accuracy, 
            'per_actor_rp_acc_distr_q1':per_actor_rp_acc_distr_q1,
        }

        return output_dict