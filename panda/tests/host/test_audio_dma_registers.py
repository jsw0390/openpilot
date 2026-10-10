"""Exercise the real panda register checker without accessing device hardware."""

from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_audio_dma_current_target_does_not_raise_register_fault():
    if shutil.which("clang") is None:
        pytest.skip("Host C compiler clang is required")
    source_path = ROOT / "panda/board/stm32h7/sound.h"
    source = source_path.read_bytes().decode("latin1")
    line = next(s for s in source.splitlines() if "register_set(&DMA1_Stream0->CR," in s)
    mask = line.rsplit(", ", 1)[1].removesuffix(");")
    assert mask == "(0x01FFFFFFU & ~DMA_SxCR_CT)", mask
    playback_line = next(s.strip() for s in source.splitlines() if "DMA1_Stream1->CR = (DMA1_Stream1->CR & ~DMA_SxCR_CT_Msk)" in s)
    assert not any("register_set(&DMA1_Stream1->CR," in s for s in source.splitlines())
    c = r"""
    #include <stdint.h>
    #include <stdbool.h>
    #include <string.h>
    #include <assert.h>
    #define ENTER_CRITICAL()
    #define EXIT_CRITICAL()
    #define FAULT_REGISTER_DIVERGENT 1U
    #define DMA_SxCR_CT (1U << 19U)
    #define DMA_SxCR_CT_Msk DMA_SxCR_CT
    #define DMA_SxCR_CT_Pos 19U
    static unsigned fault_count;
    static void print(const char *s) { (void)s; }
    static void puth(uint32_t n) { (void)n; }
    static void fault_occurred(uint32_t n) { assert(n == FAULT_REGISTER_DIVERGENT); fault_count++; }
    #include "panda/board/drivers/registers.h"
    static volatile uint32_t simulated_cr;
    typedef struct { volatile uint32_t CR; } simulated_dma_t;
    static simulated_dma_t playback_dma;
    #define DMA1_Stream1 (&playback_dma)
    static void start_playback(unsigned playback_buf, bool baseline) {
      register_clear_bits(&DMA1_Stream1->CR, 1U);
      const uint32_t before = DMA1_Stream1->CR;
      if (baseline) {
        register_set(&DMA1_Stream1->CR, (1UL - playback_buf) << DMA_SxCR_CT_Pos, DMA_SxCR_CT_Msk);
      } else {
        ACTUAL_PLAYBACK_LINE
      }
      assert((DMA1_Stream1->CR & ~DMA_SxCR_CT) == (before & ~DMA_SxCR_CT));
      assert(((DMA1_Stream1->CR & DMA_SxCR_CT) >> 19U) == (1U - playback_buf));
      register_set_bits(&DMA1_Stream1->CR, 1U);
    }
    static void check_playback(bool baseline) {
      memset(register_map, 0, sizeof(register_map));
      init_registers(); fault_count = 0U;
      playback_dma.CR = 0x00035540U;
      for (unsigned n=0U; n<1000U; n++) {
        start_playback(n % 2U, baseline);
        check_registers(); assert(fault_count == 0U);
        playback_dma.CR ^= DMA_SxCR_CT;
        check_registers();
        if (baseline) { assert(fault_count == 1U); return; }
        assert(fault_count == 0U);
      }
      playback_dma.CR ^= 1U;
      check_registers(); assert(fault_count == 1U);
    }
    static void setup(uint32_t mask) {
      memset(register_map, 0, sizeof(register_map));
      init_registers(); fault_count = 0U; simulated_cr = 0U;
      register_set(&simulated_cr, 0x00045510U, mask);
      register_set_bits(&simulated_cr, 1U);
      check_registers(); assert(fault_count == 0U);
      assert(simulated_cr == 0x00045511U);
    }
    int main(void) {
      const uint32_t original_mask = 0x01FFFFFFU;
      const uint32_t candidate_mask = ACTUAL_SOURCE_MASK;
      setup(original_mask);
      simulated_cr ^= DMA_SxCR_CT;
      assert(simulated_cr == 0x000c5511U);  // Exact logged register values.
      check_registers(); assert(fault_count == 1U);
      setup(candidate_mask);
      for (unsigned n=0; n<1000; n++) {
        simulated_cr ^= DMA_SxCR_CT;
        check_registers(); assert(fault_count == 0U);
      }
      // Every other bit monitored before this change must still detect corruption.
      for (unsigned bit=0; bit<25; bit++) {
        if (bit == 19) continue;
        setup(candidate_mask);
        simulated_cr ^= (1U << bit);
        check_registers(); assert(fault_count == 1U);
      }
      check_playback(true);
      check_playback(false);
      return 0;
    }
    """
    with tempfile.TemporaryDirectory(prefix="ray-dma-check-") as tmp:
        path = Path(tmp) / "check.c"
        binary = Path(tmp) / "check"
        path.write_text(c.replace("ACTUAL_SOURCE_MASK", mask).replace("ACTUAL_PLAYBACK_LINE", playback_line))
        compiled = subprocess.run(
            ["clang", "-std=c11", "-Wno-pointer-to-int-cast", "-I", str(ROOT), str(path), "-o", str(binary)], capture_output=True, text=True
        )
        assert compiled.returncode == 0, compiled.stderr
        checked = subprocess.run([str(binary)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


def test_microphone_shift_is_written_before_channel_enable():
    """Model the CHEN write lock while running the actual initialization lines."""
    if shutil.which("clang") is None:
        pytest.skip("Host C compiler clang is required")
    source = (ROOT / "panda/board/stm32h7/sound.h").read_bytes().decode("latin1")
    lines = [line.strip() for line in source.splitlines() if "register_" in line and "&DFSDM1_Channel3->" in line]
    assert len(lines) == 3
    assert not any("register_" in line and "&SAI1->GCR" in line for line in source.splitlines())
    actual = "\n".join(lines).replace("register_set(", "locked_register_set(")
    c = r"""
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <assert.h>
#define ENTER_CRITICAL()
#define EXIT_CRITICAL()
#define FAULT_REGISTER_DIVERGENT 1U
#define DFSDM_CHCFGR1_CHEN (1U << 7U)
#define DFSDM_CHCFGR1_SPICKSEL_Pos 2U
#define DFSDM_CHCFGR1_SITP_Pos 0U
#define DFSDM_CHCFGR2_DTRBS_Pos 3U
static unsigned fault_count;
static void print(const char *s) { (void)s; }
static void puth(uint32_t n) { (void)n; }
static void fault_occurred(uint32_t n) { assert(n == FAULT_REGISTER_DIVERGENT); fault_count++; }
#include "panda/board/drivers/registers.h"
static struct { volatile uint32_t CHCFGR1, CHCFGR2; } channel;
#define DFSDM1_Channel3 (&channel)
static void locked_register_set(volatile uint32_t *addr, uint32_t value, uint32_t mask) {
  uint32_t before = *addr;
  bool locked = addr == &channel.CHCFGR2 && (channel.CHCFGR1 & DFSDM_CHCFGR1_CHEN);
  register_set(addr, value, mask);
  if (locked) { *addr = before; }
}
static void setup(void) {
  memset(register_map, 0, sizeof(register_map)); init_registers();
  channel.CHCFGR1 = 0U; channel.CHCFGR2 = 0U; fault_count = 0U;
}
int main(void) {
  setup();
  locked_register_set(&channel.CHCFGR1, 4U | DFSDM_CHCFGR1_CHEN, 0x0000F1EFU);
  locked_register_set(&channel.CHCFGR2, 2U << DFSDM_CHCFGR2_DTRBS_Pos, 0xFFFFFFF7U);
  assert(channel.CHCFGR2 == 0U);
  check_registers(); assert(fault_count == 1U);
  setup();
  ACTUAL_CHANNEL_INIT
  assert(channel.CHCFGR2 == 0x10U);
  assert(channel.CHCFGR1 & DFSDM_CHCFGR1_CHEN);
  check_registers(); assert(fault_count == 0U);
  channel.CHCFGR2 ^= 16U;
  check_registers(); assert(fault_count == 1U);
  return 0;
}
"""
    with tempfile.TemporaryDirectory(prefix="panda-dfsdm-check-") as tmp:
        path, binary = Path(tmp) / "check.c", Path(tmp) / "check"
        path.write_text(c.replace("ACTUAL_CHANNEL_INIT", actual))
        subprocess.run(["clang", "-std=gnu11", "-Wno-pointer-to-int-cast", "-I", str(ROOT), str(path), "-o", str(binary)], check=True, capture_output=True)
        subprocess.run([str(binary)], check=True, capture_output=True)
