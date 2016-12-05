#*****************************************************#
# This file is part of GRIDOPT.                       #
#                                                     #
# Copyright (c) 2015-2016, Tomas Tinoco De Rubira.    #
#                                                     #
# GRIDOPT is released under the BSD 2-clause license. #
#*****************************************************#

import numpy as np
from types import MethodType
from numpy.linalg import norm
from .problem import TS_DCOPF_Problem
from optalg.stoch_solver import StochProblemC
from optalg.opt_solver import OptProblem,OptSolverLCCP
from scipy.sparse import csr_matrix,eye,bmat,coo_matrix,tril

class TS_DCOPF_RA_Problem(StochProblemC):
    """"
    This class represents a problem of the form
    
    minimize(p,t)   varphi_0(p) + E_r[Q(p,r)]
    subject to      E_r[ (Q(p,r)-Qmax-t)_+ + (1-gamma)t ] <= 0 { or CVaR(Q(p,r)-Qmax,gamma) }
                    p_min <= p <= p_max
    
    where Q(p,r) is the optimal value of

    minimize(q,w,s,z)   varphi_1(q)
    subjcet to          G(p+q) + Rs - Aw = b
                        p_min <= p+q <= p_max
                        z_min <= Jw <= z_max
                        0 <= s <= r.
    """

    # Parameters
    parameters = {'lam_max' : 1e2,   # max Lagrange multiplier
                  'smax_param': 1e2, # softmax parameter
                  't_reg': 1e-8,
                  't_min': -0.1,
                  't_max': 0.,
                  'Qfac': 0.8,       # factor for setting Qmax
                  'gamma': 0.95,     # parameter for CVaR (e.g. 0.95)
                  'num_samples' : 1000,
                  'num_procs': 10,
                  'tol': 1e-4,
                  'debug': False}
    
    def __init__(self,net,parameters={}):
        """
        Class constructor.
        
        Parameters
        ----------
        net : Network
        parameters : dict
        """

        # Parameters
        self.parameters = TS_DCOPF_RA_Problem.parameters.copy()
        self.set_parameters(parameters)

        # Local vars
        Qfac = self.parameters['Qfac']
        gamma = self.parameters['gamma']
 
        # Regular problem
        self.ts_dcopf = TS_DCOPF_Problem(net,self.parameters)

        # Qref and Qmax
        p_ce,gF_ce,results = self.ts_dcopf.solve_approx(quiet=True)
        self.Qref = self.ts_dcopf.eval_EQ(p_ce)[0]
        self.Fref = 0.5*np.dot(p_ce,self.ts_dcopf.H0*p_ce)+np.dot(self.ts_dcopf.g0,p_ce)+self.Qref
        self.Qmax = Qfac*self.Qref

        # Constants
        self.num_p = self.ts_dcopf.num_p
        self.num_w = self.ts_dcopf.num_w
        self.num_r = self.ts_dcopf.num_r
        self.num_bus = self.ts_dcopf.num_bus
        self.num_br = self.ts_dcopf.num_br
        self.temp_x = np.zeros(self.num_p+1) 
        self.JG_const = csr_matrix(np.hstack((np.zeros(self.num_p),1.-gamma)),shape=(1,self.temp_x.size))
        self.op = np.zeros(self.num_p)
        self.ow = np.zeros(self.num_w)
        self.os = np.zeros(self.num_r)
        self.oz = np.zeros(self.num_br)
        self.Ip = eye(self.num_p,format='coo')
        self.Iz = eye(self.num_br,format='coo')
        self.Ont = coo_matrix((self.num_bus,1))
        self.Ot = coo_matrix((1,1))
        self.Ow = coo_matrix((self.num_w,self.num_w))
        self.Os = coo_matrix((self.num_r,self.num_r))
        self.Op = coo_matrix((self.num_p,self.num_p))
        self.Oz = coo_matrix((self.num_br,self.num_br))
        self.ones_r = np.ones(self.num_r)
        self.ones_w = np.ones(self.num_w)

        # Problem
        self.Lrelaxed_approx_problem = None

    def set_parameters(self,params):
        """
        Sets problem parameters.
        
        Parameters
        ----------
        params : dict
        """
        
        for key,value in list(params.items()):
            if key in self.parameters:
                self.parameters[key] = value
        
    def eval_FG(self,x,w,problem=None,info=False):
        """
        Evaluates F, G and their subgradients at x
        for the given w.

        Parameters
        ----------
        x : (p,t)
        w : renewable powers
        
        Returns
        -------
        F : float
        gF : subgradient vector
        G : vector
        JG : csr_matrix of subgradients 
        """

        p = x[:-1]
        t = x[-1]

        gamma = self.parameters['gamma']
        num_p = self.num_p
        num_x = num_p+1
        temp_x = self.temp_x

        H0 = self.ts_dcopf.H0
        g0 = self.ts_dcopf.g0
        
        phi0 = 0.5*np.dot(p,H0*p)+np.dot(g0,p)
        gphi0 = H0*p + g0
        Q,gQ = self.ts_dcopf.eval_Q(p,w,problem=problem)

        F =  phi0+Q
        gF = np.hstack((gphi0+gQ,0.))
        
        ind = 1. if Q <= self.Qmax else 0.

        sigma = Q-self.Qmax-t
        
        G = np.array([np.maximum(sigma,0.) + (1.-gamma)*t])
        if sigma >= 0:
            temp_x[:-1] = gQ
            temp_x[-1] = -1. + (1.-gamma)
            JG = csr_matrix(temp_x,shape=(1,num_x))
        else:
            JG = self.JG_const

        if not info:
            return F,gF,G,JG
        else:
            return F,gF,G,JG,ind

    def eval_FG_approx(self,x):
        """
        Evaluates certainty-equivalent approximations
        of F and G and their derivaties.

        Parameters
        ----------
        x : (p,t)

        Returns
        -------
        F : float
        gF : gradient vector
        G : vector
        JG : Jacobian matrix
        """
        
        p = x[:-1]
        t = x[-1]
        Er = self.ts_dcopf.Er
        smax_param = self.parameters['smax_param']
        t_reg = self.parameters['t_reg']
        gamma = self.parameters['gamma']
        
        H0 = self.ts_dcopf.H0
        g0 = self.ts_dcopf.g0
        
        phi0 = 0.5*np.dot(p,H0*p)+np.dot(g0,p)
        gphi0 = H0*p + g0
        Q,gQ = self.ts_dcopf.eval_Q(p,Er)

        F =  phi0+Q+0.5*t_reg*(t**2.)
        gF = np.hstack((gphi0+gQ,t_reg*t))

        sigma = smax_param*(Q-self.Qmax-t)/self.Qref
        a = np.maximum(sigma,0.)
        C = np.exp(sigma-a)/(np.exp(-a)+np.exp(sigma-a))
        log_term = a + np.log(np.exp(-a) + np.exp(sigma-a))

        G = np.array([self.Qref*log_term/smax_param + (1.-gamma)*t])
        JG = csr_matrix(np.hstack((C*gQ,-C + 1.-gamma)),shape=(1,x.size))

        return F,gF,G,JG

    def eval_EFG_sequential(self,x,num_samples=500,seed=None,info=False):
        
        # Local vars
        p = x[:-1]
        t = x[-1]
        num_p = self.num_p
        num_w = self.num_w
        num_r = self.num_r
 
        # Seed
        if seed is None:
            np.random.seed()
        else:
            np.random.seed(seed)

        # Init
        ind = 0.
        F = 0.
        gF = np.zeros(x.size)
        G = np.zeros(1)
        JG = csr_matrix((1,x.size))
        
        # Second stage problem
        problem = self.ts_dcopf.get_problem_for_Q(p,self.ts_dcopf.Er)
        
        # Sampling loop
        for i in range(num_samples):
            
            r = self.sample_w()
            
            problem.u[num_p+num_w:num_p+num_w+num_r] = r # Important (update bound)
            
            F1,gF1,G1,JG1,ind1 = self.eval_FG(x,r,problem=problem,info=True)

            # Update
            ind += (ind1-ind)/(i+1.)
            F += (F1-F)/(i+1.)
            gF += (gF1-gF)/(i+1.)
            G += (G1-G)/(i+1.)
            JG = JG + (JG1-JG)/(i+1.)
                 
        if not info:
            return F,gF,G,JG
        else:
            return F,gF,G,JG,ind
        
    def eval_EFG(self,x,info=False):

        from multiprocess import Pool

        num_procs = self.parameters['num_procs']
        num_samples = self.parameters['num_samples']
        pool = Pool(num_procs)
        num = int(np.ceil(float(num_samples)/float(num_procs)))
        results = list(zip(*pool.map(lambda i: self.eval_EFG_sequential(x,num,i,info),range(num_procs),chunksize=1)))
        pool.terminate()
        pool.join()
        if not info:
            assert(len(results) == 4)
        else:
            assert(len(results) == 5)
        assert(all([len(vals) == num_procs for vals in results]))
        return [sum(vals)/float(num_procs) for vals in results]
        
    def get_size_x(self):

        return self.num_p + 1

    def get_init_x(self):

        x0,gF_approx,JG_approx,results = self.solve_Lrelaxed_approx(np.zeros(self.get_size_lam()),quiet=True)
        x0[-1] = self.parameters['t_min']*self.Qref
        return x0

    def get_size_lam(self):

        return 1

    def get_prop_x(self,x):
        
        p = x[:-1]
        t = x[-1]
        
        return t #self.ts_dcopf.get_prop_x(p)
        
    def project_x(self,x):
        
        p = x[:-1]
        t = x[-1]
        t_max = self.parameters['t_max']*self.Qref
        t_min = self.parameters['t_min']*self.Qref

        return np.hstack((self.ts_dcopf.project_x(p),
                          np.maximum(np.minimum(t,t_max),t_min)))

    def project_lam(self,lam):

        lmax = self.parameters['lam_max']
        return np.maximum(np.minimum(lam,lmax),0.)

    def sample_w(self):

        return self.ts_dcopf.sample_w()

    def save_x_info(self,x,filename):
       
        self.ts_dcopf.save_x_info(x[:-1],filename)
 
    def show(self):

        self.ts_dcopf.show()

        print('Fref        : %.5e' %self.Fref)
        print('Qref        : %.5e' %self.Qref)
        print('Qmax        : %.5e' %self.Qmax)
        print('Qfac        : %.2f' %self.parameters['Qfac'])
        print('gamma       : %.2f' %self.parameters['gamma'])
        print('smax param  : %.2e' %self.parameters['smax_param'])
        print('lmax        : %.2e' %self.parameters['lam_max'])
        print('t_reg       : %.2e' %self.parameters['t_reg'])
        print('t_min       : %.2e' %self.parameters['t_min'])
        print('t_max       : %.2e' %self.parameters['t_max'])
        print('num_samples : %d' %self.parameters['num_samples'])
        print('num procs   : %d' %self.parameters['num_procs'])

    def solve_Lrelaxed_approx(self,lam,g_corr=None,J_corr=None,quiet=False,init_data=None):
        """
        Solves
        
        minimize(x)   F_approx + lam^TG_approx(x) + g^Tx + lam^TJx (slope correction)
        subject to    x in X

        Returns
        -------
        x : vector
        """

        # Local vars
        t_reg = self.parameters['t_reg']
        smax_param = self.parameters['smax_param']
        gamma = self.parameters['gamma']
        prob = self.ts_dcopf
        
        # Construct problem
        problem = self.construct_Lrelaxed_approx_problem(lam,g_corr=g_corr,J_corr=J_corr)

        # Warm start
        if init_data is not None:
            problem.x = init_data['x']
            problem.lam = init_data['lam']
            problem.mu = init_data['mu']
            problem.pi = init_data['pi']

        # Solve problem
        solver = OptSolverLCCP()
        solver.set_parameters({'quiet': quiet,
                               'tol': self.parameters['tol']})
        try:
            solver.solve(problem)
            assert(solver.get_status() == 'solved')
        except Exception:
            raise
        finally:
            pass
        
        # Get results
        results = solver.get_results()
        x = results['x']
        lam = results['lam']
        mu = results['mu']
        pi = results['pi']
        
        # Check
        if self.parameters['debug']:
            A = problem.A
            b = problem.b
            l = problem.l
            u = problem.u
            problem.eval(x)
            gphi = problem.gphi
            assert(norm(gphi-A.T*lam+mu-pi) < (1e-4)*(norm(gphi)+norm(lam)+norm(mu)+norm(pi)))
            assert(norm(mu*(u-x)) < (1e-4)*(norm(mu)+norm(u-x)))
            assert(norm(pi*(x-l)) < (1e-4)*(norm(pi)+norm(x-l)))
            assert(np.all(x < u + 1e-4))
            assert(np.all(x > l - 1e-4))
            assert(norm(A*x-b) < (1e-4)*norm(b))

        # Return
        p = x[:prob.num_p]
        t = x[prob.num_p]
        q = x[prob.num_p+1:2*prob.num_p+1]
        Q = 0.5*np.dot(q,prob.H1*q)+np.dot(prob.g1,q)
        gQ = -(prob.H1*q+prob.g1)                     # See ECC paper
        sigma = smax_param*(Q-self.Qmax-t)/self.Qref
        a = np.maximum(sigma,0.)
        C = np.exp(sigma-a)/(np.exp(-a)+np.exp(sigma-a))
        gF_approx = np.hstack((prob.H0*p+prob.g0+gQ,t_reg*t))
        JG_approx = csr_matrix(np.hstack((C*gQ,-C + 1.-gamma)),shape=(1,p.size+1))
        return x[:prob.num_p+1],gF_approx,JG_approx,results
        
    def construct_Lrelaxed_approx_problem(self,lam,g_corr=None,J_corr=None):

        # Local vars
        lam = float(lam)
        Qmax = self.Qmax
        Qref = self.Qref

        smax_param = self.parameters['smax_param']
        t_reg = self.parameters['t_reg']
        t_max = self.parameters['t_max']*Qref
        t_min = self.parameters['t_min']*Qref
        gamma = self.parameters['gamma']

        prob = self.ts_dcopf
        inf = prob.parameters['infinity']

        num_p = self.num_p
        num_w = self.num_w
        num_r = self.num_r
        num_bus = self.num_bus
        num_br = self.num_br

        H0 = prob.H0
        g0 = prob.g0
        H1 = prob.H1
        g1 = prob.g1
        
        op = self.op
        ow = self.ow
        os = self.os
        oz = self.oz
        
        Ip = self.Ip
        Iz = self.Iz
        
        Ont = self.Ont
        Ot = self.Ot
        Ow = self.Ow
        Os = self.Os
        Op = self.Op
        Oz = self.Oz

        # Problem
        if self.Lrelaxed_approx_problem is not None:
            problem = self.Lrelaxed_approx_problem
        else:
                        
            # Problem construction
            A = bmat([[prob.G,Ont,prob.G,-prob.A,prob.R,None,None],
                      [Ip,None,Ip,None,None,-Ip,None],
                      [None,None,None,prob.J,None,None,-Iz]],format='coo')
            b = np.hstack((prob.b,op,oz))
            l = np.hstack((prob.p_min,           # p
                           t_min,                # t
                           -prob.p_max+prob.p_min, # q
                           -inf*self.ones_w,     # theta
                           self.os,              # s
                           prob.p_min,           # y
                           prob.z_min))          # z
            u = np.hstack((prob.p_max,           # p
                           t_max,                # t
                           prob.p_max-prob.p_min,  # q
                           inf*self.ones_w,      # theta
                           prob.Er,              # s
                           prob.p_max,           # y
                           prob.z_max))          # z
            
            problem = OptProblem()
            problem.A = A
            problem.b = b
            problem.u = u
            problem.l = l
            self.Lrelaxed_approx_problem = problem

        # Corrections
        if g_corr is None:
            g_corr = np.zeros(num_p+1)
        if J_corr is None:
            J_corr = np.zeros(num_p+1)
        else:
            J_corr = J_corr.toarray()[0,:]
        eta_p = g_corr[:-1]
        eta_t = g_corr[-1]
        nu_p = J_corr[:-1]
        nu_t = J_corr[-1]        
                
        def eval(cls,x):
            
            # Extract components
            offset = 0
            p = x[offset:offset+num_p]
            offset += num_p
            
            t = x[offset]
            offset += 1
            
            q = x[offset:offset+num_p]
            offset += num_p
            
            w = x[offset:offset+num_w]
            offset += num_w
            
            s = x[offset:offset+num_r]
            offset += num_r

            y = x[offset:offset+num_p]
            offset += num_p

            z = x[offset:]
            assert(z.size == num_br)

            # Eval partial functions
            phi0 = 0.5*np.dot(p,H0*p)+np.dot(g0,p)
            gphi0 = H0*p + g0

            phi1 = 0.5*np.dot(q,H1*q)+np.dot(g1,q)
            gphi1 = H1*q + g1
            
            beta = smax_param*(phi1-Qmax-t)/Qref
            a = np.maximum(beta,0.)
            ebma = np.exp(beta-a)
            ebm2a = np.exp(beta-2*a)
            ema = np.exp(-a)
            C1 = ebma/(ema+ebma)
            C2 = smax_param*ebm2a/(Qref*(ema*ema+2*ebm2a+ebma*ebma))
            log_term = a + np.log(ema+ebma)
            
            # Value
            cls.phi = (phi0 + 
                       phi1 +
                       0.5*t_reg*(t**2.)+
                       lam*Qref*log_term/smax_param + lam*(1-gamma)*t + 
                       np.dot(eta_p+lam*nu_p,p) + 
                       (eta_t+lam*nu_t)*t)
            
            # Gradient
            cls.gphi = np.hstack((gphi0 + eta_p + lam*nu_p, # p
                                  t_reg*t + lam*(-C1 + (1.-gamma)) + eta_t + lam*nu_t, # t
                                  (1.+lam*C1)*gphi1, # q
                                  ow,                     # theta
                                  os,                     # s
                                  op,                     # y
                                  oz))                    # z
            
            # Hessian (lower triangular)
            H = (1.+lam*C1)*H1 + tril(lam*C2*np.outer(gphi1,gphi1))
            g = gphi1.reshape((q.size,1))
            cls.Hphi = bmat([[H0,None,None,None,None,None,None],           # p
                             [None,t_reg+lam*C2,None,None,None,None,None], # t
                             [None,-lam*C2*g,H,None,None,None,None],       # q
                             [None,None,None,Ow,None,None,None],         # theta
                             [None,None,None,None,Os,None,None],         # s
                             [None,None,None,None,None,Op,None],         # y
                             [None,None,None,None,None,None,Oz]],        # z
                            format='coo')
            assert(np.all(cls.Hphi.row >= cls.Hphi.col))
            
        problem.eval = MethodType(eval,problem)
        
        return problem


