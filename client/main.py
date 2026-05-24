"""Entry point: python main.py [options]"""
import argparse
import logging


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
    parser.add_argument("--uplink", choices=["ble", "gadget", "none"], default="ble",
                        help="TX uplink backend (default: ble)")
    parser.add_argument("--key-delay-ms", type=int, default=5, metavar="MS",
                        help="Inter-keystroke delay in ms (default: 5)")
    parser.add_argument("--gadget-device", default="/dev/hidg0", metavar="PATH",
                        help="HID gadget device (gadget uplink only, default: /dev/hidg0)")
    parser.add_argument("--bridge-port", type=int, default=4243, metavar="PORT",
                        help="Local TCP port for Reticulum TCPClientInterface (default: 4243)")
    parser.add_argument("--camera", type=int, default=0, metavar="INDEX",
                        help="OpenCV camera device index (default: 0)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    uplink = _make_uplink(args)
    from gui.app import KioskApp
    KioskApp(
        bridge_port=args.bridge_port,
        camera_device=args.camera,
        uplink=uplink,
    ).run()


if __name__ == "__main__":
    main()
