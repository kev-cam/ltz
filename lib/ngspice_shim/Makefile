# Build the ngspice API shim backed by Xyce
#
# Usage:
#   make                    # build libngspice.so
#   make test               # quick smoke test
#   make install             # install to /usr/local/lib
#   make KICAD_TEST=1 test  # test with KiCad (requires X11)

XYCE_BUILD   ?= /usr/local/src/xyce-build
XYCE_SRC     ?= /usr/local/src/Xyce-8/xyce
NGSPICE_INC  ?= /usr/include

CC       = gcc
CFLAGS   = -Wall -Wextra -fPIC -O2 -g \
           -I$(NGSPICE_INC) \
           -DSHARED_MODULE

LDFLAGS  = -shared -Wl,-soname,libngspice.so.0 \
           -L$(XYCE_BUILD)/src \
           -L$(XYCE_BUILD)/utils/XyceCInterface \
           -lxycecinterface -lxyce \
           -lpthread -lm -lstdc++

TARGET   = libngspice.so.0.0.0
SONAME   = libngspice.so.0
LINKNAME = libngspice.so

SRC      = ngspice_shim.c
OBJ      = $(SRC:.c=.o)

all: $(TARGET)
	ln -sf $(TARGET) $(SONAME)
	ln -sf $(TARGET) $(LINKNAME)

$(TARGET): $(OBJ)
	$(CC) $(OBJ) -o $@ $(LDFLAGS)

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJ) $(TARGET) $(SONAME) $(LINKNAME)

test: all
	@echo "=== Smoke test ==="
	LD_LIBRARY_PATH=.:$(XYCE_BUILD)/src:$(XYCE_BUILD)/utils/XyceCInterface \
	  python3 test_shim.py

install: all
	install -d /usr/local/lib/ltz
	install -m 755 $(TARGET) /usr/local/lib/ltz/
	ln -sf $(TARGET) /usr/local/lib/ltz/$(SONAME)
	ln -sf $(TARGET) /usr/local/lib/ltz/$(LINKNAME)
	@echo "Installed to /usr/local/lib/ltz/"
	@echo "To use with KiCad: LD_PRELOAD=/usr/local/lib/ltz/libngspice.so kicad"

.PHONY: all clean test install
