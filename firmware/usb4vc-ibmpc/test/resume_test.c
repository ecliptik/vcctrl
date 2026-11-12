/*
 * Host-side test of patch 0002's pending-packet state machine.
 *
 * The functions under test are NOT re-typed here: test/run.sh cuts
 * vc_pend_drop() and vc_pend_service() verbatim out of the PATCHED main.c
 * (build/resume/src/Src/main.c) into pend_extract.c, which this file
 * #includes after mocking the four things they touch: the PS/2 byte writer,
 * the millisecond tick, micros() and the busy line. The mock bus is scripted
 * per write attempt, so each case states exactly what the host did.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define PS2_OK 0
#define PS2_ERROR_TIMEOUT 4
#define PS2_ERROR_HOST_INHIBIT 8
#define PS2_OUT_BUF_MAXSIZE 8
typedef struct { uint8_t data[PS2_OUT_BUF_MAXSIZE]; uint8_t size; } ps2_outgoing_buf;

enum { VC_EV_IN, VC_EV_BUF_FULL, VC_PKT_BUILT, VC_PKT_OK, VC_PKT_INHIBIT,
       VC_PKT_TIMEOUT, VC_PKT_PARTIAL, VC_EV_DISCARDED, VC_EDGE_MERGED,
       VC_HOST_CMD, VC_HOST_FE, VC_HOST_FF, VC_HOST_F5, VC_KB_INHIBIT_RETRY,
       VC_NCOUNTERS };
enum { VC_R_PKT_RESUMED, VC_R_INHIBIT_RETRY, VC_R_PARTIAL_WAITS,
       VC_R_SUPERSEDED, VC_R_ABANDON_BUDGET, VC_R_MAX_WAIT_MS, VC_R_NCOUNTERS };
volatile uint16_t vc_mouse_stats[VC_NCOUNTERS];
volatile uint16_t vc_resume_stats[VC_R_NCOUNTERS];
uint32_t last_mouse_movement;

/* --- mocks --- */
static uint32_t now_ms;
static uint8_t script[64]; static int nscript, iscript;   /* result per attempt */
static uint8_t wire[64]; static int nwire;               /* bytes the host got */
uint32_t HAL_GetTick(void) { return now_ms; }
uint32_t micros(void) { return now_ms * 1000; }
#define PCARD_BUSY_HI() ((void)0)
#define PCARD_BUSY_LOW() ((void)0)
uint8_t ps2mouse_write(uint8_t data, uint8_t timeout_ms)
{
  (void)timeout_ms;
  /* Past the end of the script the bus keeps doing what it last did. */
  uint8_t r = iscript < nscript ? script[iscript] : script[nscript - 1];
  iscript++;
  if (r == PS2_OK) wire[nwire++] = data;   /* only a completed byte arrives */
  return r;
}

#include "pend_extract.c"

static int fails;
#define CHECK(name, cond) do { if (cond) printf("  PASS  %s\n", name); \
  else { printf("  FAIL  %s\n", name); fails++; } } while (0)

static void start(const uint8_t *s, int n)
{
  memset((void *)vc_mouse_stats, 0, sizeof vc_mouse_stats);
  memset((void *)vc_resume_stats, 0, sizeof vc_resume_stats);
  memcpy(script, s, n); nscript = n; iscript = 0; nwire = 0; now_ms = 1000;
  vc_pend.size = 3; vc_pend.data[0] = 0x09; vc_pend.data[1] = 0x7f; vc_pend.data[2] = 0x81;
  vc_pend_idx = 0; vc_pend_hit = 0; vc_pend_start = now_ms; vc_pend_active = 1;
}
static int wire_is_packet(void)
{
  return nwire == 3 && wire[0] == 0x09 && wire[1] == 0x7f && wire[2] == 0x81;
}
/* Main-loop passes: service the pending packet until done or `max` passes,
 * advancing the clock `step` ms per pass. */
static int run(int max, uint32_t step)
{
  int passes = 0;
  while (vc_pend_active && passes < max) { vc_pend_service(); passes++; now_ms += step; }
  return passes;
}

int main(void)
{
  printf("0002 resume state machine\n");

  { uint8_t s[] = {PS2_OK, PS2_OK, PS2_OK}; start(s, 3); run(10, 1);
    CHECK("CONTROL: clean send, one pass, all 3 bytes", wire_is_packet()
          && vc_mouse_stats[VC_PKT_OK] == 1 && vc_resume_stats[VC_R_PKT_RESUMED] == 0); }

  /* The RJ failure: byte 1 out, host stalls >200 ms (many TIMEOUT passes),
   * then the bus frees. Stock would have abandoned here. */
  { uint8_t s[40]; s[0] = PS2_OK; for (int i = 1; i < 31; i++) s[i] = PS2_ERROR_TIMEOUT;
    s[31] = PS2_OK; s[32] = PS2_OK; start(s, 33); run(100, 10);
    CHECK("between-byte stall of ~300 ms: packet completes, each byte once",
          wire_is_packet());
    CHECK("  counted as resumed, not as a loss",
          vc_mouse_stats[VC_PKT_OK] == 1 && vc_mouse_stats[VC_PKT_TIMEOUT] == 0
          && vc_mouse_stats[VC_PKT_PARTIAL] == 0 && vc_resume_stats[VC_R_PKT_RESUMED] == 1);
    CHECK("  max_wait_ms recorded (~300)", vc_resume_stats[VC_R_MAX_WAIT_MS] >= 290
          && vc_resume_stats[VC_R_MAX_WAIT_MS] <= 320); }

  /* Mid-byte inhibit on byte 2: the host discarded it, so byte 2 goes again. */
  { uint8_t s[] = {PS2_OK, PS2_ERROR_HOST_INHIBIT, PS2_OK, PS2_OK}; start(s, 4); run(10, 1);
    CHECK("mid-byte inhibit on byte 2: byte 2 resent, frame intact", wire_is_packet()
          && vc_resume_stats[VC_R_INHIBIT_RETRY] == 1 && vc_resume_stats[VC_R_PKT_RESUMED] == 1); }

  /* Wait before the first byte is ordinary traffic, not a resume. */
  { uint8_t s[] = {PS2_ERROR_TIMEOUT, PS2_ERROR_TIMEOUT, PS2_OK, PS2_OK, PS2_OK}; start(s, 5); run(10, 1);
    CHECK("bus busy before byte 1: sent whole, not counted as resumed", wire_is_packet()
          && vc_resume_stats[VC_R_PKT_RESUMED] == 0 && vc_resume_stats[VC_R_PARTIAL_WAITS] == 0); }

  /* Budget with nothing sent: dropped after 1 s, no partial. */
  { uint8_t s[60]; for (int i = 0; i < 60; i++) s[i] = PS2_ERROR_TIMEOUT; start(s, 60);
    run(VC_RESUME_BUDGET_UNSENT_MS / 50 - 2, 50);
    CHECK("bus never idle, nothing sent: still pending just inside the budget",
          vc_pend_active && vc_mouse_stats[VC_PKT_TIMEOUT] == 0);
    run(10, 50);
    CHECK("  ...and dropped once past VC_RESUME_BUDGET_UNSENT_MS, no partial",
          !vc_pend_active && nwire == 0 && vc_mouse_stats[VC_PKT_TIMEOUT] == 1
          && vc_mouse_stats[VC_PKT_PARTIAL] == 0 && vc_resume_stats[VC_R_ABANDON_BUDGET] == 1); }

  /* Budget once a byte went out is 5 s, not 1 s. */
  { uint8_t s[64]; s[0] = PS2_OK; for (int i = 1; i < 64; i++) s[i] = PS2_ERROR_TIMEOUT; start(s, 64);
    run(40, 50);   /* 2 s */
    CHECK("partial packet still pending after 2 s (budget is 5 s)", vc_pend_active
          && vc_mouse_stats[VC_PKT_TIMEOUT] == 0);
    run(100, 50);  /* past 5 s */
    CHECK("  ...and dropped past 5 s, counted as timeout + partial", !vc_pend_active
          && vc_mouse_stats[VC_PKT_TIMEOUT] == 1 && vc_mouse_stats[VC_PKT_PARTIAL] == 1); }

  /* Each pass returns rather than spinning, so the keyboard gets a turn. */
  { uint8_t s[] = {PS2_OK, PS2_ERROR_TIMEOUT, PS2_ERROR_TIMEOUT, PS2_OK, PS2_OK}; start(s, 5);
    int p = run(10, 1);
    CHECK("one failed attempt per pass (returns to the main loop)", p == 3 && wire_is_packet()); }

  /* Superseded by a host command: the drop path ps2mouse_update uses. */
  { uint8_t s[] = {PS2_OK, PS2_ERROR_TIMEOUT}; start(s, 2); run(1, 1);
    vc_resume_stats[VC_R_SUPERSEDED]++; vc_pend_drop(PS2_ERROR_TIMEOUT);
    CHECK("host command mid-packet: dropped, counted timeout + partial",
          !vc_pend_active && vc_mouse_stats[VC_PKT_TIMEOUT] == 1 && vc_mouse_stats[VC_PKT_PARTIAL] == 1); }

  printf(fails ? "%d FAILED\n" : "all passed\n", fails);
  return fails != 0;
}
