USERNAME := $(shell whoami)
UUID := peripheral-battery-status@$(USERNAME)
EXTDIR := $(HOME)/.local/share/gnome-shell/extensions/$(UUID)

.PHONY: install uninstall enable disable probe

install:
	mkdir -p $(EXTDIR) $(EXTDIR)/tools
	cp extension.js README.md $(EXTDIR)/
	sed 's/@your_username/@$(USERNAME)/' metadata.json > $(EXTDIR)/metadata.json
	cp tools/legion_go_battery.py $(EXTDIR)/tools/
	-gnome-extensions enable $(UUID)

uninstall:
	-gnome-extensions disable $(UUID)
	rm -rf $(EXTDIR)

enable:
	gnome-extensions enable $(UUID)

disable:
	gnome-extensions disable $(UUID)

probe:
	gjs tools/probe.js