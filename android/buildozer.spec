# TrumpTweets — buildozer.spec
#
# QUICK START:
#   pip install buildozer cython
#   sudo apt install -y git zip unzip openjdk-17-jdk python3-pip \
#       autoconf libtool pkg-config zlib1g-dev libncurses5-dev cmake \
#       libffi-dev libssl-dev
#   buildozer android debug          # first build takes ~30 min, downloads SDK
#   adb install bin/TrumpTweets-*.apk

[app]
title           = TrumpTweets
package.name    = trumptweets
package.domain  = com.trumptweets
source.dir      = .
source.include_exts = py
version         = 1.0.0

# Kivy + KivyMD + requests (beautifulsoup not needed — HTML stripped with regex)
requirements = python3,kivy==2.2.1,kivymd==1.1.1,requests,certifi,charset-normalizer,urllib3,idna

android.permissions = INTERNET
android.api         = 33
android.minapi      = 24
android.ndk         = 25b
android.archs       = arm64-v8a

# Orientation
orientation = portrait

# Fullscreen (0 = shows status bar)
fullscreen = 0

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
