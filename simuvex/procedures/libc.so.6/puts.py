import simuvex
import symexec as se

######################################
# puts
######################################

class puts(simuvex.SimProcedure):
	def __init__(self): # pylint: disable=W0231,
		write = simuvex.SimProcedures['syscalls']['write']
		strlen = simuvex.SimProcedures['libc.so.6']['strlen']

		string = self.get_arg_expr(0)
		length = self.inline_call(strlen, string).ret_expr
		self.inline_call(write, se.BitVecVal(1, self.state.arch.bits), string, length)

		# TODO: return values
		self.exit_return()
