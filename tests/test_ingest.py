# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from survivemem import ingest

LOGS = Path(__file__).parent / "fixtures" / "logs"


def run(tool: str, name: str) -> ingest.Extract:
    return ingest.extract((LOGS / name).read_text(), tool)


def test_bitbake_rootfs() -> None:
    x = run("bitbake", "bitbake.log")
    assert x.tags == ("bitbake", "do_rootfs", "core-image-minimal")
    lines = x.excerpt.splitlines()
    assert lines[0] == "bitbake: do_rootfs failed in core-image-minimal"
    assert "| No match for argument: nonexistent-pkg" in lines
    assert lines[-1].startswith("log: /w/build/tmp/work/") and lines[-1].endswith(
        "log.do_rootfs.1234"
    )
    assert len(x.excerpt) <= 1000 and x.suggested_kind == "pitfall"


def test_bitbake_compile_error_block() -> None:
    x = run("bitbake", "bitbake-compile.log")
    assert x.tags == ("bitbake", "do_compile", "linux-socfpga")
    assert "implicit declaration of function 'devm_fpga_bridge_register'" in x.excerpt
    assert "|   CC      drivers/fpga/altera-fpga2sdram.o" in x.excerpt
    assert "| make -C" not in x.excerpt


def test_kernel_make() -> None:
    x = run("make", "kernel-make.log")
    assert x.tags == ("make", "link")
    assert 'ERROR: modpost: "fpga_bridge_get" [drivers/fpga/of-fpga-region.ko] undefined!' in (
        x.excerpt
    )
    assert "undefined reference to `fpga_bridges_enable'" in x.excerpt
    assert "Kconfig:42:warning: symbol FPGA_MGR_SOCFPGA" in x.excerpt


def test_compiler_error_location() -> None:
    text = "  CC foo.o\ndrivers/foo.c:12:5: error: unknown type name 'u33'\n   12 | u33 x;\n"
    x = ingest.extract(text, "make")
    assert x.tags == ("make", "compile", "drivers/foo.c")
    assert x.excerpt.startswith("drivers/foo.c:12:5: error:")


def test_dmesg() -> None:
    x = run("dmesg", "dmesg.log")
    assert set(x.tags) == {"dmesg", "oops", "probe", "eprobe_defer", "fpga"}
    assert "Internal error: Oops: 805 [#1] SMP ARM" in x.excerpt
    assert "fpga_manager fpga0: Error after writing image data to FPGA" in x.excerpt
    assert "Booting Linux" not in x.excerpt and len(x.excerpt) <= 1000


def test_quartus_fitter() -> None:
    x = run("quartus", "quartus-fit.log")
    assert x.tags[:2] == ("quartus", "fitter") and "11802" in x.tags and "169085" in x.tags
    assert "Error (11802): Can't fit design in device." in x.excerpt
    assert "Error: Quartus Prime Fitter was unsuccessful. 2 errors, 2 warnings" in x.excerpt


def test_quartus_timing_summary() -> None:
    x = run("quartus", "quartus-sta.log")
    assert "timing" in x.tags
    assert "setup slack -1.274 clk_50" in x.excerpt
    assert "hold slack 0.183 clk_50" in x.excerpt


def test_quartus_programmer_and_jtag_chain() -> None:
    pgm = run("quartus", "quartus-pgm.log")
    assert "Error (209025): Can't recognize silicon ID for device 2." in pgm.excerpt
    assert "cable: DE-SoC [1-1.2]" in pgm.excerpt
    chain = run("quartus", "jtagconfig.log")
    assert chain.suggested_kind == "environment" and "jtag" in chain.tags
    assert "02D020DD   5CSEBA6(.|ES)/5CSEBA6ES/.." in chain.excerpt


def test_openocd() -> None:
    x = run("openocd", "openocd.log")
    assert set(x.tags) == {"openocd", "jtag", "target-not-examined"}
    assert "Error: JTAG scan chain interrogation failed: all ones" in x.excerpt
    assert "Error: [fpgasoc.cpu.0] Examination failed" in x.excerpt
    assert "Info : This adapter doesn't support configurable speed" in x.excerpt  # tail


def test_secrets_are_redacted_and_excerpt_capped() -> None:
    text = "ERROR: x-1.0-r0 do_fetch: token ghp_" + "a" * 36 + "\n" + "| error " * 2000
    x = ingest.extract(text, "bitbake")
    assert "ghp_" not in x.excerpt and "[REDACTED]" in x.excerpt and len(x.excerpt) <= 1000


@pytest.mark.parametrize(
    ("command", "tool"),
    [
        ("cd build && bitbake core-image-minimal 2>&1 | tail", "bitbake"),
        ("devtool build linux-socfpga", "bitbake"),
        ("make -j8 ARCH=arm zImage", "make"),
        ("cmake --build .", "other"),
        ("sudo dmesg -w", "dmesg"),
        ("journalctl -k -b -1", "dmesg"),
        ("quartus_sh --flow compile top", "quartus"),
        ("jtagconfig", "quartus"),
        ("openocd -f board.cfg", "openocd"),
        ("arm-none-eabi-gdb vmlinux", "gdb"),
        ("ls -la", "other"),
    ],
)
def test_detect_tool(command: str, tool: str) -> None:
    assert ingest.detect_tool(command) == tool


def test_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        ingest.extract("x", "vivado")


def test_device_tree_compiler_error() -> None:
    text = (
        "  DTC     arch/arm/boot/dts/socfpga_cyclone5_de10_nano.dtb\n"
        "Error: arch/arm/boot/dts/socfpga_cyclone5_de10_nano.dts:42.1-2 syntax error\n"
        "FATAL ERROR: Unable to parse input tree\n"
        "make[2]: *** [scripts/Makefile.lib:423: arch/arm/boot/dts/x.dtb] Error 1\n"
    )
    x = ingest.extract(text, "make")
    assert x.tags == ("make", "dtc", "arch/arm/boot/dts/socfpga_cyclone5_de10_nano.dts")
    assert x.excerpt.startswith("Error: arch/arm/boot/dts/socfpga_cyclone5_de10_nano.dts:42.1-2")
