"""Entry point: python main.py [options]"""
import argparse
import logging
import os


def _make_uplink(args):
    if args.uplink == "ble":
        from tx.ble import BleUplink
        return BleUplink(key_delay_ms=args.key_delay_ms)
    if args.uplink == "gadget":
        from tx.gadget import GadgetUplink
        return GadgetUplink(device=args.gadget_device, key_delay_ms=args.key_delay_ms)
    from tx.ble import NullUplink
    return NullUplink()


def main():
    parser = argparse.ArgumentParser(description="Reticulum kiosk bridge")
    parser.add_argument("--mode", choices=["legacy", "webhid"], default="legacy",
                        help="Transport mode: 'legacy' (the standard camera+keyboard path, works "
                             "on any browser) or 'webhid' (vendor HID, fast "
                             "path, needs WebHID-capable kiosk browser)")
    parser.add_argument("--uplink", choices=["ble", "gadget", "none"], default="ble",
                        help="TX uplink backend (default: ble)")
    parser.add_argument("--key-delay-ms", type=int, default=5, metavar="MS",
                        help="Inter-keystroke delay in ms (standard mode only; default: 5)")
    parser.add_argument("--gadget-device", default="/dev/hidg0", metavar="PATH",
                        help="HID gadget device (gadget uplink only, default: /dev/hidg0)")
    parser.add_argument("--bridge-port", type=int, default=4243, metavar="PORT",
                        help="Local TCP port for Reticulum TCPClientInterface (default: 4243)")
    parser.add_argument("--camera", type=int, default=0, metavar="INDEX",
                        help="OpenCV camera device index (standard mode only; default: 0)")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    if args.mode == "webhid" and args.uplink == "gadget":
        parser.error("--mode webhid requires --uplink ble (gadget path is keyboard-only)")

    uplink = _make_uplink(args)
    from gui.app import KioskApp
    KioskApp(
        bridge_port=args.bridge_port,
        camera_device=args.camera,
        uplink=uplink,
        mode=args.mode,
    ).run()


if __name__ == "__main__":
    main()
