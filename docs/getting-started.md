# Getting started

## 1. Install the host tools

Use Python 3.10 or newer. Install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) from Android's official distribution and put `adb` on your PATH. The repository does not include SDK executables, phone firmware, APKs, or virtual environments.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
adb version
```

For iOS workers, install the optional Python dependencies with `python -m pip install -e '.[ios]'`, then install Appium and its XCUITest driver from their official distributions. Prepare Xcode signing and WebDriverAgent for your own devices. Keep signing-team identifiers and UDIDs in private configuration.

## 2. Create private configuration

The normal configuration directory is `~/.config/pokemon-go-automation`. `POGO_CONFIG_DIR` can point to another private directory. Initialize generic templates with:

```sh
pogo init-config
# Or explicitly choose another private location:
pogo init-config --directory "$HOME/.config/pokemon-go-automation"
```

The command creates a disabled device registry and placeholder profiles. It preserves existing files. Fill in private identifiers, measure the relevant coordinates, and enable only the devices and operations you have configured. The public files in `examples/` mirror these templates. [Configuration](configuration.md) explains how the profiles refer to one another.

`--devices all` also discovers connected Android phones whose serials are absent from the registry. Disabled placeholder templates do not prevent those unregistered phones from being selected. A disabled record excludes its matching real device once you have supplied that identifier. To limit an initial run, name the intended device explicitly or disconnect other phones. Android workflow calibration is checked when its worker connects; a selection plan alone does not validate that calibration. Plans list configured devices without probing phones; they do not enumerate extra unregistered Android phones that execution may discover. Use live fleet status to inspect connected hardware and explicit device selection to constrain the run.

Do not put your completed examples back into the checkout. Choose aliases such as `android-one` and `ios-one`, then fill in your real identifiers only in your private files.

## 3. Connect Android phones

1. Enable Developer options and USB debugging on each phone.
2. Connect a USB cable and approve the computer's debugging prompt on the phone.
3. Run `adb devices` and verify the phone is listed as `device`.
4. Calibrate the workflow buttons for the actual screen layout.
5. If a worker reads its calibration from the phone, upload your private calibration to the expected on-device path.

For the legacy Android trade calibration, the path is `/storage/self/primary/AutoTraderConfig.yaml`. The generic template explains the expected coordinate names. Use the phone's Pointer location display to measure coordinates; match your resolution, display scaling, orientation, and current game UI.

## 4. Connect iOS phones

Use your private Appium profile to identify the device and configure the Appium server and WebDriverAgent connection. Verify that WebDriverAgent and Appium can see the phone before attempting an automation. An Appium server responding to `/status` does not by itself prove that a specific phone is connected or that an Appium session will open.

Configure separate calibration files where workflows require them. Keep the phone unlocked and display the documented starting screen.

## 5. Inspect, plan, then run one supervised action

```sh
python fleet.py status
python send_gifts.py --devices all --count 1 --plan
```

Read the selected devices and computer routing. If correct, prepare the Friends screen and run a single gift cycle:

```sh
python send_gifts.py --devices all --count 1
```

Watch the physical phones, verify the resulting state, and inspect local logs. Increase the count only after the small run behaves as expected. See [Testing](testing.md) for the evidence to retain privately.

## Troubleshooting

| Symptom | Check next |
| --- | --- |
| Missing fleet file | Check `POGO_CONFIG_DIR` and create `pokemon-fleet.yaml` from the examples. |
| Android unauthorized/offline | Check the USB cable, unlock the phone, approve debugging, then rerun `adb devices`. |
| iOS server is reachable but phone fails | Verify the UDID, device trust, signing, WebDriverAgent, and Appium session independently. |
| A tap lands on the wrong control | Stop the run and recalibrate the affected screen. |
| A remote worker cannot start | Check SSH, its private config directory, installed checkout, and Python path. |
| A worker stops at a screen guard | Inspect that screen and its detection/calibration; do not disable guards as the first fix. |
