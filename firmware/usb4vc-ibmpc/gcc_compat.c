/* GCC/newlib compatibility for upstream code written against Keil microlib.
 *
 * Built into EVERY variant, stock included, because it is a toolchain
 * difference and not a behaviour change: under microlib, printf reaches the
 * UART through upstream's own fputc() (main.c). Under newlib it goes through
 * _write() instead, which --specs=nosys.specs stubs to fail silently. The one
 * caller is spi_error_dump_reboot()'s "SPI ERROR" dump on USART1, just before
 * the board reboots itself. Without this, the GCC build would lose the only
 * diagnostic that path produces.
 */
#include <stdio.h>

int fputc(int ch, FILE *f);   /* upstream's, in main.c */

int _write(int fd, const char *buf, int len)
{
  (void)fd;
  for (int i = 0; i < len; i++)
    fputc(buf[i], stdout);
  return len;
}
