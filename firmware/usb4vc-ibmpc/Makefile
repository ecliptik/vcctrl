# GCC build of USB4VC's IBM PC protocol-board firmware (STM32F072C8).
#
#   ./fetch.sh                  once: pinned upstream source + ST startup
#   make VARIANT=stock          upstream ae3813d, UNMODIFIED -- the baseline
#   make VARIANT=counters       + patches/*.patch, applied in order
#
# Output: build/<VARIANT>/ibmpc.hex (and .elf, .bin, .map).
#
# VARIANT=stock exists so a GCC build can be told apart from a patch: Keil
# produced the stock hex, GCC produces a different binary from the same
# source, and if the board misbehaves after a flash, the first question is
# "is it the patch or the compiler". Flash stock-GCC first and the question
# answers itself.

VARIANT ?= counters
B := build
SRC := $(B)/$(VARIANT)/src
OUT := $(B)/$(VARIANT)

CC := arm-none-eabi-gcc
OBJCOPY := arm-none-eabi-objcopy
SIZE := arm-none-eabi-size

# Upstream's Keil options (ibmpc.uvprojx): USE_HAL_DRIVER,STM32F072xB, C99,
# optimisation level 4 (-O3), microlib. -O2 here, not -O3: every PS/2 bit
# delay is timed off TIM2 (delay_us.c), so the protocol does not depend on
# instruction timing, and -O2 is the level GCC's own embedded users run.
MCU := -mcpu=cortex-m0 -mthumb
CFLAGS := $(MCU) -std=gnu99 -O2 -g -Wall -ffunction-sections -fdata-sections \
          -DUSE_HAL_DRIVER -DSTM32F072xB \
          -I$(SRC)/Inc -I$(SRC)/Drivers/STM32F0xx_HAL_Driver/Inc \
          -I$(SRC)/Drivers/STM32F0xx_HAL_Driver/Inc/Legacy \
          -I$(SRC)/Drivers/CMSIS/Device/ST/STM32F0xx/Include \
          -I$(SRC)/Drivers/CMSIS/Include
LDFLAGS := $(MCU) -T STM32F072C8_FLASH.ld --specs=nano.specs --specs=nosys.specs \
           -Wl,--gc-sections -Wl,-Map=$(OUT)/ibmpc.map -Wl,--print-memory-usage

# Exactly the Keil project's file list.
HAL := hal hal_cortex hal_dma hal_flash hal_flash_ex hal_gpio hal_i2c \
       hal_i2c_ex hal_iwdg hal_pwr hal_pwr_ex hal_rcc hal_rcc_ex hal_spi \
       hal_spi_ex hal_tim hal_tim_ex hal_uart hal_uart_ex
APP := delay_us helpers main mcp4451 ps2kb ps2mouse stm32f0xx_hal_msp \
       stm32f0xx_it system_stm32f0xx xt_kb
CSRC := $(foreach m,$(HAL),$(SRC)/Drivers/STM32F0xx_HAL_Driver/Src/stm32f0xx_$(m).c) \
        $(foreach m,$(APP),$(SRC)/Src/$(m).c)
OBJS := $(patsubst $(SRC)/%.c,$(OUT)/obj/%.o,$(CSRC)) $(OUT)/obj/startup.o \
        $(OUT)/obj/gcc_compat.o

PATCHES := $(if $(filter stock,$(VARIANT)),,$(sort $(wildcard patches/*.patch)))

all: $(OUT)/ibmpc.hex
	@$(SIZE) $(OUT)/ibmpc.elf
	@sha256sum $(OUT)/ibmpc.hex

# The source tree is rebuilt from the pinned checkout whenever a patch
# changes, so a stale half-patched tree can never be what gets compiled.
$(SRC)/.stamp: $(B)/upstream/firmware/ibmpc/Src/main.c $(PATCHES)
	rm -rf $(SRC) $(OUT)/obj
	mkdir -p $(SRC)
	cp -r $(B)/upstream/firmware/ibmpc/Inc $(B)/upstream/firmware/ibmpc/Src \
	      $(B)/upstream/firmware/ibmpc/Drivers $(SRC)/
	for p in $(PATCHES); do patch -s -p1 -d $(SRC) < $$p || exit 1; done
	touch $@

# The sources do not exist until the stamp rule copies them in; say so, or
# make treats the object pattern rule as inapplicable.
$(CSRC): $(SRC)/.stamp ;

$(B)/upstream/firmware/ibmpc/Src/main.c:
	@echo "run ./fetch.sh first" >&2; exit 1

$(OUT)/obj/%.o: $(SRC)/%.c $(SRC)/.stamp
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/obj/gcc_compat.o: gcc_compat.c $(SRC)/.stamp
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(OUT)/obj/startup.o: $(B)/startup_stm32f072xb.s $(SRC)/.stamp
	@mkdir -p $(dir $@)
	$(CC) $(MCU) -c $< -o $@

$(OUT)/ibmpc.elf: $(OBJS) STM32F072C8_FLASH.ld
	$(CC) $(OBJS) $(LDFLAGS) -o $@

$(OUT)/ibmpc.hex: $(OUT)/ibmpc.elf
	$(OBJCOPY) -O ihex $< $@
	$(OBJCOPY) -O binary $< $(OUT)/ibmpc.bin

clean:
	rm -rf $(B)/stock $(B)/counters

.PHONY: all clean
