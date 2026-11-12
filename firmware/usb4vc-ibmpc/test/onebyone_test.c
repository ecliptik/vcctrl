/*
 * Host-side test of patch 0003 (one event per packet, original spacing).
 * test/run-onebyone.sh cuts the REAL functions out of the patched sources:
 * the mouse_buf ring from helpers.c, and cap_to_127 through
 * get_consolidated_mouse_event (with 0003's pacing block) from main.c.
 * This file supplies only the types, the clock and a scripted backlog.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define MOUSE_EVENT_BUFFER_SIZE 16
typedef struct { int32_t movement_x, movement_y; int8_t scroll_vertical, scroll_horizontal;
  uint8_t button_left, button_middle, button_right, button_side, button_extra; } mouse_event;
typedef struct { uint8_t head, tail; mouse_event* mouse_events; } mouse_buf;
volatile uint32_t vc_ev_arrival[MOUSE_EVENT_BUFFER_SIZE];
static uint32_t now_us;
uint32_t micros(void) { return now_us; }

#include "extract_helpers.c"
#include "extract_main.c"

static int fails;
#define CHECK(name, cond) do { if (cond) printf("  PASS  %s\n", name); \
  else { printf("  FAIL  %s\n", name); fails++; } } while (0)

static mouse_event store[MOUSE_EVENT_BUFFER_SIZE];
static mouse_buf q;
typedef struct { uint32_t t; int32_t x, y; uint8_t b; } pkt;
static pkt out[64]; static int nout;

static void reset(void)
{
  memset(&q, 0, sizeof q); q.mouse_events = store; nout = 0;
  vc_sent_any = 0; vc_last_sent_arrival = 0; vc_last_sent_at = 0;
}
static void enqueue(uint32_t t_ms, int32_t x, int32_t y, uint8_t left)
{
  mouse_event e; memset(&e, 0, sizeof e);
  e.movement_x = x; e.movement_y = y; e.button_left = left;
  vc_ev_arrival[q.head] = t_ms * 1000;
  mouse_buf_add(&q, &e);
}
/* What ps2mouse_update does each pass, minus the wire: peek, pacing check,
 * consolidate. Runs the main loop at 1 ms steps from `from_ms` to `to_ms`. */
static void drain(uint32_t from_ms, uint32_t to_ms)
{
  for (now_us = from_ms * 1000; now_us <= to_ms * 1000; now_us += 1000) {
    if (mouse_buf_peek(&q) == NULL) continue;
    if (!vc_spacing_ok(&q, micros())) continue;
    mouse_event c; get_consolidated_mouse_event(&q, &c);
    out[nout].t = now_us / 1000; out[nout].x = c.movement_x;
    out[nout].y = c.movement_y; out[nout].b = c.button_left; nout++;
  }
}
/* One MS3 press as vcctrl sends it: 4 moves 50 ms apart, down, 200 ms dwell, up. */
static void press(uint32_t t0, int32_t tx)
{
  enqueue(t0, 127, -127, 0); enqueue(t0 + 50, 127, -127, 0);
  enqueue(t0 + 100, tx, 127, 0); enqueue(t0 + 150, 0, 15, 0);
  enqueue(t0 + 200, 0, 0, 1); enqueue(t0 + 412, 0, 0, 0);
}

int main(void)
{
  printf("0003 one event per packet, original spacing\n");

  /* CONTROL: no backlog. Events drained as they arrive add no delay. */
  reset();
  for (int i = 0; i < 6; i++) { enqueue(i * 50, 1, 0, 0); drain(i * 50, i * 50); }
  int ok = nout == 6;
  for (int i = 0; ok && i < 6; i++) ok = out[i].t == (uint32_t)(i * 50);
  CHECK("CONTROL: no backlog, each event leaves the moment it arrives", ok);

  /* The RKM1 press 91 case: a 1,247 ms stall. Press A's 6 events and press
   * B's (starting 1,200 ms later) are all queued when the bus frees. */
  reset();
  press(0, -42); press(1200, -75);
  drain(1250, 4000);
  CHECK("12 queued events leave as 12 packets (nothing summed)", nout == 12);
  ok = out[0].x == 127 && out[2].x == -42 && out[3].y == 15 && out[4].b == 1
       && out[5].b == 0 && out[6].x == 127 && out[8].x == -75;
  CHECK("in FIFO order: A's moves, A's down, A's up, then B's moves", ok);
  ok = 1;
  for (int i = 1; i < 4; i++) ok = ok && (out[i].t - out[i - 1].t) >= 50;
  CHECK("A's moves keep their 50 ms spacing (one per game poll)", ok);
  CHECK("A's down-to-up keeps its 212 ms dwell", out[5].t - out[4].t >= 212);
  CHECK("the idle gap A-up -> B is shortened to the 250 ms cap",
        out[6].t - out[5].t == 250);
  CHECK("B's down-to-up keeps its dwell too", out[11].t - out[10].t >= 212);

  /* Overflow guard: at 14 queued, consolidate (stopping at a button edge). */
  reset();
  for (int i = 0; i < 13; i++) enqueue(i, 10, 0, 0);
  enqueue(13, 0, 0, 1);
  drain(100, 100);
  CHECK("at 14 queued: the 13 moves are summed into one packet, stopping at the down",
        nout == 1 && out[0].x == 127 && out[0].b == 0);

  printf(fails ? "%d FAILED\n" : "all passed\n", fails);
  return fails != 0;
}
