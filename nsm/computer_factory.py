"""Computers can read in tokens, parse them into a program, and execute it."""

from __future__ import print_function

import json
from collections import OrderedDict
import re
import copy
import sys
import os
import nsm.data_utils as data_utils
import pprint

END_TK = data_utils.END_TK  # End of program token
ERROR_TK = '<ERROR>'
# SPECIAL_TKS = [END_TK, ERROR_TK, '(', ')']
SPECIAL_TKS = [ERROR_TK, '(', ')']


class LispInterpreter(object):
    """Interpreter reads in tokens, parse them into a program and execute it."""

    def __init__(self, type_hierarchy, max_mem, max_n_exp, assisted=True):
        """
        max_mem: maximum number of memory slots.
        max_n_exp: maximum number of expressions.
        assisted: whether to provide assistance to the programmer (used for neural programmer).
        """
        # Create namespace.
        self.namespace = Namespace()

        self.assisted = assisted
        # Configs.
        # Functions used to call
        # Signature: autocomplete(evaled_exp, valid_tokens, evaled_tokens)
        # return a subset of valid_tokens that passed the
        # filter. Used to implement filtering with denotation.
        self.type_hierarchy = type_hierarchy
        self.type_ancestry = create_type_ancestry(type_hierarchy)
        self.max_mem = max_mem
        self.max_n_exp = max_n_exp

        # Initialize the parser state.
        self.n_exp = 0
        self.history = []
        self.exp_stack = []
        self.done = False
        self.result = None

    @property
    def primitive_names(self):
        primitive_names = []
        for k, v in self.namespace.iteritems():
            if ('property' in self.type_ancestry[v['type']] or
                    'primitive_function' in self.type_ancestry[v['type']]):
                primitive_names.append(k)
        return primitive_names

    @property
    def primitives(self):
        primitives = []
        for k, v in self.namespace.iteritems():
            if ('property' in self.type_ancestry[v['type']] or
                    'primitive_function' in self.type_ancestry[v['type']]):
                primitives.append(v)
        return primitives

    def add_constant(self, value, type, name=None):
        """Generate the code and variables to hold the constants."""
        if name is None:
            name = self.namespace.generate_new_name()
        self.namespace[name] = dict(
            value=value, type=type,
            is_constant=True)
        return name

    def add_function(self, name, value, args, return_type,
                     autocomplete, type):
        """Add function into the namespace."""
        if name in self.namespace:
            raise ValueError('Name %s is already used.' % name)
        else:
            self.namespace[name] = dict(
                value=value, type=type,
                autocomplete=autocomplete,
                return_type=return_type, args=args)

    def autocomplete(self, exp, tokens, token_vals, namespace):
        # func = util[0]
        # util = [x['value'] for x in util]
        # token_vals = [x['value'] for x in token_vals]
        # if func['type'] == 'global_primitive_function':
        #   return func['autocomplete'](
        #     util, tokens, token_vals, namespace=namespace)
        # else:
        #   return func['autocomplete'](util, tokens, token_vals)
        function = exp[0]

        return function['autocomplete'](exp, tokens, token_vals)

    def reset(self, only_reset_variables=False):
        """Reset all the interpreter state."""
        if only_reset_variables:
            self.namespace.reset_variables()
        else:
            self.namespace = Namespace()
        self.history = []
        self.n_exp = 0
        self.exp_stack = []
        self.done = False
        self.result = None

    def read_token_id(self, token_id):
        token = self.rev_vocab[token_id]
        return self.read_token(token)

    def read_token(self, token):
        """Read in one token, parse and execute the expression if completed."""
        if ((self.n_exp >= self.max_n_exp) or
                (self.namespace.n_var >= self.max_mem)):
            token = END_TK
        new_exp = self.parse_step(token)
        # If reads in end of program, then return the last value as result.
        if token == END_TK:
            self.done = True
            self.result = self.namespace.get_last_value()
            return self.result
        elif new_exp:
            if self.assisted:
                name = self.namespace.generate_new_name()
                result = self.eval(['define', name, new_exp])
                self.n_exp += 1
                # If there are errors in the execution, self.eval
                # will return None. We can also give a separate negative
                # reward for errors.
                if result is None:
                    self.namespace.n_var -= 1
                    self.done = True
                    self.result = [ERROR_TK]
                #   result = self.eval(['define', name, ERROR_TK])
            else:
                result = self.eval(new_exp)
            return result
        else:
            return None

    def valid_tokens(self):
        """Return valid tokens for the next step for programmer to pick."""
        # If already exceeded max memory or max expression
        # limit, then must end the program.
        if ((self.n_exp >= self.max_n_exp) or
                (self.namespace.n_var >= self.max_mem)):
            result = [END_TK]
        # If last expression is finished, either start a new one
        # or end the program.
        elif not self.history:
            result = ['(']
        # If not in an expression, either start a new expression or end the program.
        elif not self.exp_stack:
            result = ['(', END_TK]
        # If currently in an expression.
        else:
            exp = self.exp_stack[-1]
            # If in the middle of a new expression.
            if exp:
                # Use number of arguments to check if all arguments are there.
                head = exp[0]
                args = self.namespace[head]['args']
                pos = len(exp) - 1
                if pos == len(args):
                    result = [')']
                else:
                    result = self.namespace.valid_tokens(
                        args[pos], self.get_type_ancestors)
                    if self.autocomplete is not None:
                        valid_tokens = result
                        evaled_exp = [self.eval(item) for item in exp]
                        evaled_tokens = [self.eval(tk) for tk in valid_tokens]
                        result = self.autocomplete(
                            evaled_exp, valid_tokens, evaled_tokens, self.namespace)
            # If at the beginning of a new expression.
            else:
                result = self.namespace.valid_tokens(
                    {'types': ['head']}, self.get_type_ancestors)
                # remove expression like (hop v v) (filter_str_contain_any)
                # if self.n_exp == 1 and self.history[1] == 'hop':
                #     result.remove('filter_str_contain_any')
        return result

    def parse_step(self, token):
        """Run the parser for one step with given token which parses tokens into expressions."""
        self.history.append(token)
        if token == END_TK:
            self.done = True
        elif token == '(':
            self.exp_stack.append([])
        elif token == ')':
            # One list is finished.
            new_exp = self.exp_stack.pop()
            if self.exp_stack:
                self.exp_stack[-1].append(new_exp)
            else:
                self.exp_stack = []
                return new_exp
        elif self.exp_stack:
            self.exp_stack[-1].append(token)
        else:
            # Atom expression.
            return token

    def tokenize(self, chars):
        """Convert a string of characters into a list of tokens."""
        return chars.replace('(', ' ( ').replace(')', ' ) ').split()

    def get_type_ancestors(self, type):
        return self.type_ancestry[type]

    def infer_type(self, return_type, arg_types):
        """Infer the type of the returned value of a function."""
        if hasattr(return_type, '__call__'):
            return return_type(*arg_types)
        else:
            return return_type

    def eval(self, x, namespace=None):
        """Another layer above _eval to handle exceptions."""
        try:
            result = self._eval(x, namespace)
        except Exception as e:
            print('Error: ', e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            print('when evaluating ', x)
            print(self.history)
            pprint.pprint(self.namespace)
            raise e
            result = None
        return result

    def _eval(self, x, namespace=None):
        """Evaluate an expression in an namespace."""
        if namespace is None:
            namespace = self.namespace
        if is_symbol(x):  # variable reference
            return namespace.get_object(x).copy()
        elif x[0] == 'define':  # (define name exp)
            (_, name, exp) = x
            obj = self._eval(exp, namespace)
            print(name,obj) #debug
            namespace[name] = obj
            return obj
        else:
            # Execute a function.
            proc = self._eval(x[0], namespace)
            args = [self._eval(exp, namespace) for exp in x[1:]]
            arg_values = [arg['value'] for arg in args]
            if proc['type'] == 'global_primitive_function':
                arg_values += [self.namespace]
            value = proc['value'](*(arg_values))
            arg_types = [arg['type'] for arg in args]
            type = self.infer_type(proc['return_type'], arg_types)
            return {'value': value, 'type': type, 'is_constant': False}

    def step(self, token):
        """Open AI gym inferface."""
        result = self.read_token(token)
        observation = token
        reward = 0.0
        done = self.done
        if (result is None) or self.done:
            write_pos = None
        else:
            write_pos = self.namespace.n_var - 1

        info = {'result': result,
                'write_pos': write_pos}
        return observation, reward, done, info

    def get_last_var_loc(self):
        return self.namespace.n_var - 1

    def interactive(self, prompt='> ', assisted=False):
        """A prompt-read-eval-print loop."""
        print('Namespace:')
        for key, val in self.namespace.items():
            if val['type'] == 'primitive_function':
                print('Function: {}'.format(key))
            else:
                print('Entity: {}, Value: {}'.format(key, val))

        temp = self.assisted
        # try:
        self.assisted = assisted
        while True:
            # try:
            query = input(prompt).strip()
            tokens = self.tokenize(query)
            for tk in tokens:
                result = self.read_token(tk)
                print('Read in [{}], valid tokens: {}'.format(tk, self.valid_tokens()))
                if result:
                    print('Result:')
                    print(json.dumps(result, indent=2))
            # except Exception as e:
            #   print(e)
            #   continue
        # finally:
        #   self.assisted = temp

    def has_extra_work(self):
        """Check if the current solution contains some extra/wasted work."""
        all_var_names = ['v{}'.format(i)
                         for i in range(self.namespace.n_var)]
        for var_name in all_var_names:
            obj = self.namespace.get_object(var_name)
            # If some variable is not given as constant, not used
            # in other expressions and not the last one, then
            # generating it is some extra work that should not be
            # done.
            if ((not obj['is_constant']) and
                    (var_name not in self.history) and
                    (var_name != self.namespace.last_var)):
                return True
        return False

    def clone(self):
        """Make a copy of itself, used in search."""
        new = LispInterpreter(
            self.type_hierarchy, self.max_mem, self.max_n_exp, self.assisted)

        new.history = self.history[:]
        new.exp_stack = copy.deepcopy(self.exp_stack)
        new.n_exp = self.n_exp
        new.namespace = self.namespace.clone()
        return new

    def get_vocab(self):
        mem_tokens = []
        for i in range(self.max_mem):
            mem_tokens.append('v{}'.format(i))
        vocab = data_utils.Vocab(
            list(self.namespace.get_all_names()) + SPECIAL_TKS + mem_tokens)
        return vocab


class Namespace(OrderedDict):
    """Namespace is a mapping from names to values.

  Namespace maintains the mapping from names to their
  values. It also generates new variable names for memory
  slots (v0, v1...), and support finding a subset of
  variables that fulfill some type constraints, (for
  example, find all the functions or find all the entity
  lists).
  """

    def __init__(self, *args, **kwargs):
        """Initialize the namespace with a list of functions."""
        # params = dict(zip(names, objs))
        super(Namespace, self).__init__(*args, **kwargs)
        self.n_var = 0
        self.last_var = None

    def clone(self):
        new = Namespace(self)
        new.n_var = self.n_var
        new.last_var = self.last_var
        return new

    def clone_and_reset(self):
        copy = self.clone()
        copy.reset_variables()

        return copy

    def generate_new_name(self):
        """Create and return a new variable."""
        name = 'v{}'.format(self.n_var)
        self.last_var = name
        self.n_var += 1
        return name

    def valid_tokens(self, constraint, get_type_ancestors):
        """Return all the names/tokens that fulfill the constraint."""
        return [k for k, v in self.items()
                if self._is_token_valid(v, constraint, get_type_ancestors)]

    def _is_token_valid(self, token, constraint, get_type_ancestors):
        """Determine if the token fulfills the given constraint."""
        type = token['type']
        return set(get_type_ancestors(type) + [type]).intersection(constraint['types'])

    def get_value(self, name):
        return self[name]['value']

    def get_object(self, name):
        return self[name]

    def get_last_value(self):
        if self.last_var is None:
            return None
        else:
            return self.get_value(self.last_var)

    def get_all_names(self):
        return self.keys()

    def reset_variables(self):
        keys = list(self.keys())
        for k in keys:
            if re.match(r'v\d+', k):
                del self[k]
        self.n_var = 0
        self.last_var = None


def is_symbol(x):
    return isinstance(x, str)


def create_type_ancestry(type_tree):
    type_ancestry = {}
    for type, _ in type_tree.items():
        _get_type_ancestors(type, type_tree, type_ancestry)
    return type_ancestry


def _get_type_ancestors(type, type_hrchy, type_ancestry):
    """Compute the ancestors of a type with memorization."""
    if type in type_ancestry:
        return type_ancestry[type]
    else:
        parents = type_hrchy[type]
        result = parents[:]
        for p in parents:
            ancestors = _get_type_ancestors(p, type_hrchy, type_ancestry)
            for a in ancestors:
                if a not in result:
                    result.append(a)
        type_ancestry[type] = result
        return result
