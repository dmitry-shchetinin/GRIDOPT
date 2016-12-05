#*****************************************************#
# This file is part of GRIDOPT.                       #
#                                                     #
# Copyright (c) 2015-2016, Tomas Tinoco De Rubira.    #
#                                                     #
# GRIDOPT is released under the BSD 2-clause license. #
#*****************************************************#

from .method import TS_DCOPF_Method
from .problem_risk import TS_DCOPF_Problem
from .problem_risk import TS_DCOPF_RA_Problem
from optalg.stoch_solver import StochHybridPD

class TS_DCOPF_PDSH(TS_DCOPF_Method):
    """
    Primal-Dual Stochatic Hybrid Approximation method 
    for two-stage risk-averse DC OPF problem.
    """
    
    parameters = {'quiet': False}

    def __init__(self):

        TS_DCOPF_Method.__init__(self)
        self.parameters = TS_DCOPF_PDSH.parameters.copy()
        self.parameters.update(TS_DCOPF_Problem.parameters)
        self.parameters.update(TS_DCOPF_RA_Problem.parameters)
        self.parameters.update(StochHybridPD.parameters)

        self.problem = None
        self.results = None

    def get_name(self):
        
        name = 'Primal-Dual Stochatic Hybrid Approximation'
        if not self.parameters['no_G']:
            return name
        else:
            return name + ' - No G'
 
    def create_problem(self,net,parameters):
        
        return TS_DCOPF_RA_Problem(net,parameters)
        
    def solve(self,net):
        
        # Local variables
        params = self.parameters
        
        # Parameters
        quiet = params['quiet']
        
        # Problem
        self.problem = self.create_problem(net,params)
        if not quiet:
            self.problem.show()
            
        # Solver
        solver = StochHybridPD()
        solver.set_parameters(params)
        
        # Solve
        solver.solve(self.problem)
        
        # Results
        self.results = solver.get_results()
        
