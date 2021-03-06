import idascript

import sys
import os
import traceback

import logging
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler(stream=sys.stdout)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

try:
    import idautils
    import idaapi
    import sark

    print os.path.dirname(__file__)
    sys.path.insert(0, os.path.dirname(__file__))
    from avr_utils import *

    def make_stack_variable(func_start, offset, name, size):
        func = idaapi.get_func(func_start)
        frame = idaapi.get_frame(func)
        if frame is None:
            if idaapi.add_frame(func, 0, 0, 0) == -1:
                raise ValueError("couldn't create frame for function @ 0x%x" % func_start)
            frame = idaapi.get_frame(func)

        offset += func.frsize
        member = idaapi.get_member(frame, offset)

        if member:
            return 0
        else:
            # No member at the offset, create a new one
            if idaapi.add_struc_member(frame,
                    name,
                    offset,
                    idaapi.wordflag() if size == 2 else idaapi.byteflag(),
                    None, size) == 0:
                return 1
            else:
                raise ValueError("failed to create stack frame member %s @ +0x%x in function @ 0x%x" % (name, offset, func_start))

    def get_stack_variable_name(func_start, offset):
        func = idaapi.get_func(func_start)
        frame = idaapi.get_frame(func)
        if frame is None:
            raise ValueError("couldn't get frame for function @ 0x%x" % func_start)

        offset += func.frsize
        member = idaapi.get_member(frame, offset)
        return idaapi.get_member_name(member.id)

    def is_latter_of_stack_sequential_instructions(prev_line, curr_line, op_num):
        other_op_num = 0
        if op_num == 0:
            other_op_num = 1

        if not is_latter_of_rxN_sequential_instructions(prev_line, curr_line, other_op_num):
            return False

        try:
            curr_insn = curr_line.insn
            prev_insn = prev_line.insn
        except sark.exceptions.SarkNoInstruction:
            return False

        if (str(prev_insn.operands[op_num]).startswith('Y+') and
            prev_insn.operands[op_num].offset == curr_insn.operands[op_num].offset - 1
           ):
            logger.debug("offsets 0x%x && 0x%x are sequential: %s && %s" % (prev_insn.operands[op_num].offset, curr_insn.operands[op_num].offset, prev_line, curr_line))
            return True

        return False

    def doesnt_imply_stack_var(curr_insn):
        return str(curr_insn.operands[1].reg) == 'r1'

    def operand_to_stack_variable(curr_line, op_num):
        logger.debug("setting stack var operand %d of %s" % (op_num, curr_line))
        idc.OpStkvar(curr_line.ea, op_num)
        return

    def get_next_line(line):
        return sark.Line(line.ea + len(line.bytes))

    def create_stack_variable_from_operand(curr_line, op_num):
        try:
            curr_insn = curr_line.insn
        except sark.exceptions.SarkNoInstruction:
            return

        if doesnt_imply_stack_var(curr_insn):
            return

        stack_offset = curr_insn.operands[op_num].offset
        this_function = sark.Function(curr_line.ea)

        next_line = get_next_line(curr_line)
        size = 2 if is_latter_of_stack_sequential_instructions(curr_line, next_line, op_num) else 1

        logger.info("creating %d byte stack variable @ 0x%x based on %s && %s" % (size, stack_offset, curr_line, next_line))

        make_stack_variable(this_function.startEA, stack_offset, "var_%x" % stack_offset, size)
        return

    def is_sensible_instruction(insn):
        return (len(insn.operands) == 2 and
                str(insn.operands[0]) != '' and str(insn.operands[1]) != ''
               )

    def all_y_stack_vars_here():
        ea = idc.here()
        this_function = sark.Function(ea)

        prev = None
        for line in this_function.lines:
            if prev is None:
                prev = line
                continue

            try:
                curr_insn = line.insn
                prev_insn = prev.insn
            except sark.exceptions.SarkNoInstruction:
                logger.debug("skipping @ 0x%x" % line.ea)
                prev = line
                continue

            if (len(curr_insn.operands) != 2 or
                str(curr_insn.operands[0]) == '' or str(curr_insn.operands[1]) == ''
                ):
                logger.debug("filtering: %s" % (line))
                prev = line
                continue

            logger.debug("testing %s" % (line))

            if str(curr_insn.operands[1]).startswith('Y+'):
                operand_to_stack_variable(line, 1)
                if not is_latter_of_stack_sequential_instructions(prev, line, 1): # avoid marking a stack var for the second part of a sequential load
                    create_stack_variable_from_operand(line, 1)

            if str(curr_insn.operands[0]).startswith('Y+'):
                operand_to_stack_variable(line, 0)
                if not is_latter_of_stack_sequential_instructions(prev, line, 0): # avoid marking a stack var for the second part of a sequential load
                    create_stack_variable_from_operand(line, 0)

            prev = line

        for a1st_line in this_function.lines:
            a2nd_line = get_next_line(a1st_line)
            a3rd_line = get_next_line(a2nd_line)

            if a2nd_line.ea > this_function.endEA or a3rd_line.ea > this_function.endEA:
                continue

            try:
                a1st_insn = a1st_line.insn
                a2nd_insn = a2nd_line.insn
                a3rd_insn = a3rd_line.insn
            except sark.exceptions.SarkNoInstruction:
                logger.debug("skipping %s && %s && %s" % (a1st_line, a2nd_line, a3rd_line))
                continue

            if (not is_sensible_instruction(a1st_insn)) or (not is_sensible_instruction(a2nd_insn)) or (not is_sensible_instruction(a3rd_insn)):
                logger.debug("filtering %s && %s && %s" % (a1st_insn, a2nd_insn, a3rd_insn))
                continue

            if (str(a1st_insn.mnem) == 'movw' and (str(a1st_insn.operands[1]) == 'YL' or str(a1st_insn.operands[1].reg) == 'r28') and
                str(a1st_insn.operands[0]) == str(a2nd_insn.operands[0]) and a2nd_insn.operands[0].reg == avr_get_register_pairs().get(a3rd_insn.operands[0].reg) and
                str(a2nd_insn.mnem) == 'subi' and str(a3rd_insn.mnem) == 'sbci'
               ):
                stack_offset = -1 * int(a2nd_insn.operands[1].text, 0)
                this_function = sark.Function(a1st_line.ea)

                # TODO: guess size of stack var
                size = 1
                logger.info("creating %d byte stack variable @ 0x%x based on %s && %s && %s" % (size, stack_offset, a1st_line, a2nd_line, a3rd_line))

                make_stack_variable(this_function.startEA, stack_offset, "var_%x" % stack_offset, size)

                name = get_stack_variable_name(this_function.startEA, stack_offset)
                idc.OpAlt(a2nd_line.ea, 1, "-%s" % name)

    print("some utility functions are defined:\nall_y_stack_vars_here()")

except:
    exc_type, exc_value, exc_traceback = sys.exc_info()
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

idascript.exit()
