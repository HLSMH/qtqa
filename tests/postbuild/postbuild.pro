TEMPLATE = subdirs
SUBDIRS += \
    bic \
    headers \
    symbols \
    guiapplauncher

!qtConfig(process): SUBDIRS -= headers guiapplauncher

# This test is only valid on linux
!linux: SUBDIRS -= symbols

# This test does not make sense with '-no-widgets'
!qtHaveModule(widgets): SUBDIRS -= bic
