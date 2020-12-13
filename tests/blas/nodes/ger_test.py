#!/usr/bin/env python3
# Copyright 2019-2020 ETH Zurich and the DaCe authors. All rights reserved.

import numpy as np

import argparse
import scipy
import random

import dace
from dace.memlet import Memlet

import dace.libraries.blas as blas
import dace.libraries.blas.utility.fpga_helper as streaming
from dace.libraries.blas.utility import memory_operations as memOps
from dace.transformation.interstate import GPUTransformSDFG

from dace.libraries.standard.memory import aligned_ndarray

from multiprocessing import Process, Queue


# ---------- ----------
# FPGA graph by column
# ---------- ----------
def fpga_graph_column(veclen, n_tile, m_tile, precision, vendor, testCase="0"):

    DATATYPE = precision

    n = dace.symbol("n")
    m = dace.symbol("m")
    a = dace.symbol("alpha")

    rowTile = m_tile
    colTile = n_tile

    vendor_mark = "x" if vendor == "xilinx" else "i"
    test_sdfg = dace.SDFG("ger_test_" + vendor_mark + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_symbol(a.name, DATATYPE)

    vec_type = dace.vector(precision, veclen)
    single_vec_type = dace.vector(precision, 1)

    test_sdfg.add_array('A', shape=[n*m/veclen], dtype=vec_type)
    test_sdfg.add_array('x', shape=[m], dtype=single_vec_type)
    test_sdfg.add_array('y', shape=[n/veclen], dtype=vec_type)
    test_sdfg.add_array('r', shape=[n*m/veclen], dtype=vec_type)

    A_stream = streaming.StreamReadMatrixFull(
        'A',
        m,
        n,
        rowTile,
        colTile,
        DATATYPE,
        blockByRow=False,
        tileByRow=True,
        veclen=veclen
    )

    y_stream = streaming.StreamReadVector(
        'y',
        n,
        DATATYPE,
        # repeat='{}/{}'.format(m, rowTile),
        veclen=veclen
    )

    x_stream = streaming.StreamReadVector(
        'x',
        m,
        DATATYPE,
        repeat='{}/{}'.format(n, colTile),
    )

    res_stream = streaming.StreamWriteMatrixFull(
        'r',
        m,
        n,
        rowTile,
        colTile,
        DATATYPE,
        blockByRow=False,
        tileByRow=True,
        veclen=veclen
    )


    ger_node = blas.Ger(
        "blas_ger",
        dtype=DATATYPE,
        n_tile = colTile,
        m_tile = rowTile,
        n=n,
        m=m,
        veclen=veclen,
        alpha=a
    )
    ger_node.implementation = 'fpga_column'

    preState, postState = streaming.fpga_setup_connect_streamers(
        test_sdfg,
        test_state,
        ger_node,
        [x_stream, y_stream, A_stream],
        ['_x', '_y', '_A'],
        ger_node,
        [res_stream],
        ['_res']
    )

    test_sdfg.expand_library_nodes()

    mode = "simulation" if vendor == "xilinx" else "emulator"
    dace.config.Config.set("compiler", "fpga_vendor", value=vendor)
    dace.config.Config.set("compiler", vendor, "mode", value=mode)

    return test_sdfg


def fpga_graph_array_column(veclen, n_tile, m_tile, precision, vendor, testCase="0"):

    DATATYPE = precision

    n = dace.symbol("n")
    m = dace.symbol("m")
    a = dace.symbol("alpha")

    vendor_mark = "x" if vendor == "xilinx" else "i"
    test_sdfg = dace.SDFG("ger_test_array_" + vendor_mark + "_" + testCase)

    test_sdfg.add_symbol(a.name, DATATYPE)

    vec_type = dace.vector(precision, veclen)
    single_vec_type = dace.vector(precision, 1)

    test_sdfg.add_array('A', shape=[n*m/veclen], dtype=vec_type)
    test_sdfg.add_array('x', shape=[m], dtype=single_vec_type)
    test_sdfg.add_array('y', shape=[n/veclen], dtype=vec_type)
    test_sdfg.add_array('r', shape=[n*m/veclen], dtype=vec_type)

    ###########################################################################
    # Copy data to FPGA

    copy_in_state = test_sdfg.add_state("copy_to_device")

    in_host_x = copy_in_state.add_read("x")
    in_host_y = copy_in_state.add_read("y")
    in_host_A = copy_in_state.add_read("A")

    test_sdfg.add_array("device_x",
                        shape=[m],
                        dtype=single_vec_type,
                        storage=dace.dtypes.StorageType.FPGA_Global,
                        transient=True)
    test_sdfg.add_array("device_y",
                        shape=[n/veclen],
                        dtype=vec_type,
                        storage=dace.dtypes.StorageType.FPGA_Global,
                        transient=True)
    test_sdfg.add_array("device_A",
                        shape=[n*m/veclen],
                        dtype=vec_type,
                        storage=dace.dtypes.StorageType.FPGA_Global,
                        transient=True)

    in_device_x = copy_in_state.add_write("device_x")
    in_device_y = copy_in_state.add_write("device_y")
    in_device_A = copy_in_state.add_write("device_A")

    copy_in_state.add_memlet_path(in_host_x,
                                  in_device_x,
                                  memlet=Memlet.simple(in_host_x,
                                                       "0:{}".format(m)))
    copy_in_state.add_memlet_path(in_host_y,
                                  in_device_y,
                                  memlet=Memlet.simple(in_host_y,
                                                       "0:{}".format(n/veclen)))
    copy_in_state.add_memlet_path(in_host_A,
                                  in_device_A,
                                  memlet=Memlet.simple(in_host_A,
                                                       "0:{}".format(n*m/veclen)))

    ###########################################################################
    # Copy data from FPGA
    copy_out_state = test_sdfg.add_state("copy_to_host")

    test_sdfg.add_array("device_r",
                        shape=[n*m/veclen],
                        dtype=vec_type,
                        storage=dace.dtypes.StorageType.FPGA_Global,
                        transient=True)

    out_device = copy_out_state.add_read("device_r")
    out_host = copy_out_state.add_write("r")

    copy_out_state.add_memlet_path(out_device,
                                   out_host,
                                   memlet=Memlet.simple(out_host,
                                                        "0:{}".format(n*m/veclen)))

    ########################################################################
    # FPGA State

    fpga_state = test_sdfg.add_state("fpga_state")

    x = fpga_state.add_read("device_x")
    y = fpga_state.add_read("device_y")
    A = fpga_state.add_read("device_A")
    z = fpga_state.add_write("device_r")

    ger_node = blas.Ger(
        "blas_ger",
        dtype=DATATYPE,
        n_tile = n_tile,
        m_tile = m_tile,
        n=n,
        m=m,
        veclen=veclen,
        alpha=a
    )
    ger_node.implementation = 'fpga_column'

    fpga_state.add_memlet_path(x,
                               ger_node,
                               dst_conn="_x",
                               memlet=Memlet.simple(x, "0:{}".format(m)))
    fpga_state.add_memlet_path(y,
                               ger_node,
                               dst_conn="_y",
                               memlet=Memlet.simple(y, "0:{}".format(n/veclen)))
    fpga_state.add_memlet_path(A,
                               ger_node,
                               dst_conn="_A",
                               memlet=Memlet.simple(A, "0:{}".format(n*m/veclen)))
    fpga_state.add_memlet_path(ger_node,
                               z,
                               src_conn="_res",
                               memlet=Memlet.simple(z, "0:{}".format(n*m/veclen)))

    ######################################
    # Interstate edges
    test_sdfg.add_edge(copy_in_state, fpga_state,
                       dace.sdfg.sdfg.InterstateEdge())
    test_sdfg.add_edge(fpga_state, copy_out_state,
                       dace.sdfg.sdfg.InterstateEdge())

    #########
    # Validate
    test_sdfg.fill_scope_connectors()
    test_sdfg.validate()

    # test_sdfg.~~~()
    ger_node.expand(test_sdfg, fpga_state)

    mode = "simulation" if vendor == "xilinx" else "emulator"
    dace.config.Config.set("compiler", "fpga_vendor", value=vendor)
    dace.config.Config.set("compiler", vendor, "mode", value=mode)

    return test_sdfg


# ---------- ----------
# Pure graph program (CPU)
# ---------- ----------
def pure_graph(dtype):

    n = dace.symbol("n")
    m = dace.symbol("m")

    sdfg = dace.SDFG(
        "ger_operation")  # rank 1 operation: r = alpha * x * yT + A

    state = sdfg.add_state("ger")

    sdfg.add_symbol("alpha", dtype)

    sdfg.add_array("x", shape=[m], dtype=dtype)
    sdfg.add_array("y", shape=[n], dtype=dtype)
    sdfg.add_array("A", shape=[m, n], dtype=dtype)
    sdfg.add_array("r", shape=[m, n], dtype=dtype)  # result

    x = state.add_read("x")
    y = state.add_read("y")
    A = state.add_read("A")
    result = state.add_write("r")

    ger_node = blas.Ger(name="ger", dtype=dtype)
    ger_node.implementation = "pure"

    state.add_memlet_path(x,
                          ger_node,
                          dst_conn="_x",
                          memlet=Memlet.simple(x, "0:m", num_accesses=m))
    state.add_memlet_path(y,
                          ger_node,
                          dst_conn="_y",
                          memlet=Memlet.simple(y, "0:n", num_accesses=n))
    state.add_memlet_path(A,
                          ger_node,
                          dst_conn="_A",
                          memlet=Memlet.simple(A,
                                               "0:m, 0:n",
                                               num_accesses=m * n))
    state.add_memlet_path(ger_node,
                          result,
                          src_conn="_res",
                          memlet=Memlet.simple(result,
                                               "0:m, 0:n",
                                               num_accesses=m * n))

    sdfg.validate()
    return sdfg



def run_test(ger, target):

    x = np.ndarray(m, dtype=np.float32)
    y = np.ndarray(n, dtype=np.float32)
    A = np.ndarray((m, n), dtype=np.float32)
    result = np.ndarray((m, n), dtype=np.float32)

    x[:] = np.random.rand(m).astype(np.float32)
    y[:] = np.random.rand(n).astype(np.float32)
    A[:] = np.random.rand(m, n).astype(np.float32)

    ger(alpha=alpha, x=x, y=y, A=A, r=result, m=m, n=n)

    ref = scipy.linalg.blas.sger(alpha=alpha, x=x, y=y, a=A)

    diff = np.linalg.norm(np.subtract(result, ref))
    if diff >= args.eps * n * m:
        raise RuntimeError("Unexpected result returned from ger rank 1 operation: "
              "got:\n{}\nexpected:\n{} on {}".format(result, ref, target))
    else:
        print("Ok")



if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("N", type=int, nargs="?", default=64)
    parser.add_argument("M", type=int, nargs="?", default=64)
    parser.add_argument("n_tile", type=int, nargs="?", default=16)
    parser.add_argument("m_tile", type=int, nargs="?", default=16)
    parser.add_argument("alpha", type=np.float32, nargs="?", default=1.0)
    parser.add_argument("--target", dest="target", default="pure")
    parser.add_argument("--eps", type=float, default=1e-6)
    parser.add_argument("--veclen", type=int, default=2)
    args = parser.parse_args()
    n = args.N
    m = args.M
    n_tile = args.n_tile
    m_tile = args.m_tile
    alpha = args.alpha
    veclen = args.veclen

    if args.target == "pure":
        sdfg = pure_graph(dace.float32)
        run_test(sdfg.compile(), args.target)
    elif args.target == "intel_fpga":

        # Test streaming
        sdfg = fpga_graph_column(veclen, n_tile, m_tile, dace.float32, args.target, "0")
        run_test(sdfg.compile(), args.target)

        # TODO: for Intel need to run multiple tests in different processes, see e.g. axpy_tests
        # else beatiful Intel tools will crash

        # Test array based
        # sdfg = fpga_graph_array_column(veclen, n_tile, m_tile, dace.float32, args.target, "0")
        # run_test(sdfg.compile(), args.target)

    elif args.target == "xilinx":

        # Test streaming
        sdfg = fpga_graph_column(veclen, n_tile, m_tile, dace.float32, args.target, "0")
        run_test(sdfg.compile(), args.target)

        # Test array based
        sdfg = fpga_graph_array_column(veclen, n_tile, m_tile, dace.float32, args.target, "0")
        run_test(sdfg.compile(), args.target)

    else:
        print("Unsupported target")
        exit(-1)


    

