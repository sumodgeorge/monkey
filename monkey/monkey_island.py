from monkey_island.cc.main import main


def parse_cli_args():
    import argparse
    parser = argparse.ArgumentParser(description="Infection Monkey Island CnC Server. See https://infectionmonkey.com")
    parser.add_argument("-s", "--setup-only", action="store_true",
                        help="Pass this flag to cause the Island to setup and exit without actually starting. This is useful "
                             "for preparing Island to boot faster later-on, so for compiling/packaging Islands.")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Pass this flag to start interactive debugging.")
    args = parser.parse_args()
    if args.debug:
        import pdb
        pdb.set_trace()
    return args.setup_only


if "__main__" == __name__:
    is_setup_only = parse_cli_args()
    main(is_setup_only)
