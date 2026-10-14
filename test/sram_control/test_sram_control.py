import random
 
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.utils import get_sim_time
 

 
SIG = {
    "clk": "clk",
    "rst_n": "rst_n",
    # I/O Unit write request channel (testbench drives, except ready)
    "wr_wen": "io_wr_req_wen",
    "wr_addr": "io_wr_req_waddr",
    "wr_data": "io_wr_req_wdata",
    "wr_valid": "io_wr_req_valid",
    "wr_ready": "io_wr_req_ready",
    # RTU read request channel (testbench drives, except ready)
    "rd_addr": "rtu_rd_req_raddr",
    "rd_valid": "rtu_rd_req_valid",
    "rd_ready": "rtu_rd_req_ready",
    # RTU read response channel (controller drives, except ready)
    "resp_data": "rtu_rd_resp_rdata",
    "resp_valid": "rtu_rd_resp_valid",
    "resp_ready": "rtu_rd_resp_ready",
    # SRAM side (controller drives addr/din/wen, testbench drives dout)
    "sram_addr": "addr",
    "sram_din": "din",
    "sram_wen": "wen",
    "sram_dout": "dout",
}
 
EXPECTED_CYCLES_PER_ACCESS = 2   
TIMEOUT = 50                     
CLK_PERIOD_NS = 10
 
 

 
def now():
    """Current clock-cycle number (rising edges are at multiples of the period)."""
    return int(get_sim_time("ns") // CLK_PERIOD_NS)
 
 
class Env:
    def __init__(self, dut):
        self.dut = dut
        self.mem = {}            # SRAM contents as seen by the model
        self.sram_writes = []    # (cycle, addr, data) every time the SRAM sees wen=1
        self.resp = []           # (cycle, data) every completed read-response transfer
 
    def h(self, name):
        return getattr(self.dut, SIG[name])
 
    def get(self, name, default=0):
        """Read a signal as int; X/Z reads as `default`."""
        try:
            return int(self.h(name).value)
        except ValueError:
            return default
 
    def set(self, name, value):
        self.h(name).value = value
 
 
async def background(env):
    """
    Runs every cycle for the whole test:
      * acts as the SRAM (synchronous, read-first), logging every write it sees
      * logs every completed transfer on the read-response channel
    """
    while True:
        await RisingEdge(env.h("clk"))
        a = env.get("sram_addr")
        d = env.get("sram_din")
        w = env.get("sram_wen")
        old = env.mem.get(a, 0)          # unwritten addresses read as 0 in this model
 
        if w:
            env.mem[a] = d
            env.sram_writes.append((now(), a, d))
 
        if env.get("resp_valid") and env.get("resp_ready"):
            env.resp.append((now(), env.get("resp_data")))
 
        await Timer(1, "ns")
        env.set("sram_dout", old)        # visible from the next cycle (1-cycle latency)
 
 
async def init(dut):
    """Start clock, SRAM model and monitor, apply reset, return the Env."""
    env = Env(dut)
    for name in ("wr_wen", "wr_addr", "wr_data", "wr_valid", "rd_addr", "rd_valid"):
        env.set(name, 0)
    env.set("resp_ready", 1)
    env.set("sram_dout", 0)
    env.set("rst_n", 0)
 
    cocotb.start_soon(Clock(env.h("clk"), CLK_PERIOD_NS, units="ns").start())
    cocotb.start_soon(background(env))
 
    for _ in range(5):
        await RisingEdge(env.h("clk"))
    await Timer(1, "ns")
    env.set("rst_n", 1)
    await RisingEdge(env.h("clk"))
    await Timer(1, "ns")
    return env
 
 
async def idle(env, cycles):
    for _ in range(cycles):
        await RisingEdge(env.h("clk"))
    await Timer(1, "ns")
 

 
async def io_write(env, addr, data, wen=1, timeout=TIMEOUT):
    """
    Send one write request and wait for it to be accepted (valid & ready).
    Returns the cycle of acceptance. valid is dropped after the transfer, but
    if you call io_write again straight away valid stays high (back-to-back).
    """
    env.set("wr_wen", wen)
    env.set("wr_addr", addr)
    env.set("wr_data", data)
    env.set("wr_valid", 1)
    for _ in range(timeout):
        await RisingEdge(env.h("clk"))
        if env.get("wr_ready"):
            t = now()
            await Timer(1, "ns")
            env.set("wr_valid", 0)
            return t
    raise AssertionError(f"write to addr {addr} not accepted within {timeout} cycles")
 
 
async def rtu_read(env, addr, timeout=TIMEOUT):
    """Send one read request, wait for acceptance, then wait for the response data."""
    n0 = len(env.resp)
    env.set("rd_addr", addr)
    env.set("rd_valid", 1)
    for _ in range(timeout):
        await RisingEdge(env.h("clk"))
        if env.get("rd_ready"):
            break
    else:
        raise AssertionError(f"read of addr {addr} not accepted within {timeout} cycles")
    await Timer(1, "ns")
    env.set("rd_valid", 0)
 
    for _ in range(timeout):
        if len(env.resp) > n0:
            return env.resp[n0][1]
        await RisingEdge(env.h("clk"))
        await Timer(1, "ns")
    raise AssertionError(f"no read response for addr {addr} within {timeout} cycles")
 
 
async def rtu_read_stream(env, addrs, timeout=TIMEOUT):
    """
    Issue reads back-to-back WITHOUT waiting for each response (pipelined).
    Returns (accept_cycles, data_list).
    """
    n0 = len(env.resp)
    accepts = []
    idx = 0
    env.set("rd_addr", addrs[0])
    env.set("rd_valid", 1)
    waited = 0
    while idx < len(addrs):
        await RisingEdge(env.h("clk"))
        if env.get("rd_ready"):
            accepts.append(now())
            idx += 1
            waited = 0
        else:
            waited += 1
            assert waited <= timeout, f"read {idx} not accepted within {timeout} cycles"
        await Timer(1, "ns")
        if idx < len(addrs):
            env.set("rd_addr", addrs[idx])
        else:
            env.set("rd_valid", 0)
 
    for _ in range(timeout):
        if len(env.resp) - n0 >= len(addrs):
            break
        await RisingEdge(env.h("clk"))
        await Timer(1, "ns")
    assert len(env.resp) - n0 == len(addrs), (
        f"expected {len(addrs)} responses, got {len(env.resp) - n0}"
    )
    return accepts, [d for _, d in env.resp[n0:]]
 
 
def unique_value(addr):
    """Address-derived data so aliasing between addresses is detected."""
    return (addr * 0x9E37 + 0x1234) & 0xFFFF
 

 
@cocotb.test()
async def test_write_then_read_all_addresses(dut):
    """Write every address through the I/O channel, read back through the RTU channel."""
    env = await init(dut)
    depth = 2 ** len(env.h("sram_addr"))
 
    for a in range(depth):
        await io_write(env, a, unique_value(a))
    await idle(env, 3)
    assert len(env.sram_writes) == depth, (
        f"expected {depth} SRAM writes, saw {len(env.sram_writes)}"
    )
 
    for a in range(depth):
        got = await rtu_read(env, a)
        assert got == unique_value(a), (
            f"addr {a}: expected 0x{unique_value(a):04X}, got 0x{got:04X}"
        )
 
 
@cocotb.test()
async def test_boundary_addresses(dut):
    """Address 0 and the top address are routed correctly."""
    env = await init(dut)
    last = 2 ** len(env.h("sram_addr")) - 1
 
    await io_write(env, 0, 0xBEEF)
    await io_write(env, last, 0xCAFE)
    assert await rtu_read(env, 0) == 0xBEEF
    assert await rtu_read(env, last) == 0xCAFE
 
 
@cocotb.test()
async def test_back_to_back_writes_in_order(dut):
    """Back-to-back writes all land at the SRAM, in order, none lost or duplicated."""
    env = await init(dut)
 
    expected = [(a, unique_value(a)) for a in range(32)]
    for a, d in expected:
        await io_write(env, a, d)
    await idle(env, 5)
 
    seen = [(a, d) for _, a, d in env.sram_writes]
    assert seen == expected, "SRAM writes differ from requests (lost, reordered or duplicated)"
 
 
@cocotb.test()
async def test_back_to_back_reads_in_order(dut):
    """Pipelined reads return the right data, in order, one response per request."""
    env = await init(dut)
    for a in range(32):
        env.mem[a] = unique_value(a)
 
    addrs = [3, 17, 3, 0, 31, 8, 9, 10]
    _, data = await rtu_read_stream(env, addrs)
    assert data == [unique_value(a) for a in addrs]
    assert env.sram_writes == [], "reads must never write the SRAM"
 
 
@cocotb.test()
async def test_throughput_cycles_per_access(dut):
    """Accepted requests are spaced EXPECTED_CYCLES_PER_ACCESS apart (docs: 2 cycles/word)."""
    if EXPECTED_CYCLES_PER_ACCESS is None:
        return
    env = await init(dut)
 
    # writes
    ts = [await io_write(env, a, a) for a in range(16)]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert all(g == EXPECTED_CYCLES_PER_ACCESS for g in gaps), f"write gaps: {gaps}"
 
    # reads: 17 words = scene header, expected 17 * 2 = 34 cycles
    for a in range(17):
        env.mem[a] = unique_value(a)
    accepts, _ = await rtu_read_stream(env, list(range(17)))
    gaps = [b - a for a, b in zip(accepts, accepts[1:])]
    assert all(g == EXPECTED_CYCLES_PER_ACCESS for g in gaps), f"read gaps: {gaps}"
 
 
@cocotb.test()
async def test_random_operations_against_model(dut):
    """Random writes and reads (never simultaneous) compared with a Python model."""
    env = await init(dut)
    rng = random.Random(1234)
    pool = rng.sample(range(2 ** len(env.h("sram_addr"))), 64)
    model = {}
 
    for a in pool:
        v = rng.getrandbits(16)
        await io_write(env, a, v)
        model[a] = v
 
    for i in range(1000):
        a = rng.choice(pool)
        if rng.random() < 0.5:
            v = rng.getrandbits(16)
            await io_write(env, a, v)
            model[a] = v
        else:
            got = await rtu_read(env, a)
            assert got == model[a], f"op {i}: addr {a} got 0x{got:04X}, expected 0x{model[a]:04X}"
 
    await idle(env, 5)
    for a in pool:
        assert env.mem[a] == model[a], f"SRAM model mismatch at addr {a}"
 