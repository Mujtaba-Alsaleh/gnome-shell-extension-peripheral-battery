USERNAME := $(shell whoami)
UUID := peripheral-battery-status@$(USERNAME)
EXTDIR := $(HOME)/.local/share/gnome-shell/extensions/$(UUID)
UNIT_NAME := peripheral-battery-legion.service
UNIT_DIR := $(HOME)/.config/systemd/user
UNIT_FILE := $(UNIT_DIR)/$(UNIT_NAME)

.PHONY: install uninstall enable disable probe

install:
	mkdir -p $(EXTDIR) $(EXTDIR)/tools $(UNIT_DIR)
	cp extension.js README.md $(EXTDIR)/
	sed 's/@your_username/@$(USERNAME)/' metadata.json > $(EXTDIR)/metadata.json
	cp tools/legion_go_battery.py $(EXTDIR)/tools/
	@printf '[Unit]\nDescription=Legion Go controller battery helper (Peripheral Battery Status)\n# Reads the docked controllers raw HID report and writes\n# ~/.cache/peripheral-battery-status/legion-go.json for the extension.\n# Runs outside the graphical session so it survives shell restarts.\n\n[Service]\nType=simple\nExecStart=/usr/bin/python3 $(EXTDIR)/tools/legion_go_battery.py\nRestart=always\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n' > $(UNIT_FILE)
	-systemctl --user daemon-reload
	-systemctl --user enable --now $(UNIT_NAME)
	-gnome-extensions enable $(UUID)

uninstall:
	-gnome-extensions disable $(UUID)
	-systemctl --user disable --now $(UNIT_NAME)
	rm -rf $(EXTDIR)
	rm -f $(UNIT_FILE)

enable:
	gnome-extensions enable $(UUID)

disable:
	gnome-extensions disable $(UUID)

probe:
	gjs tools/probe.js