import arguments

from testConfig import TestConfig
from trafficFlowTests import TrafficFlowTests


def main() -> None:
    args = arguments.parse_args()
    tc = TestConfig(args.config)
    tft = TrafficFlowTests(tc)

    for test in tft._tc.GetConfig():
        print("Starting run")
        tft.run(test, args.evaluator_config)
        print("Finished run")
        print(f"Args: {args}")
        print(f"Evaluating: {args.evaluator_config}")
        if args.evaluator_config:
            print("Evaluation started")
            if not tft.evaluate_run_success():
                print(f"Failure detected in {test['name']} results")


if __name__ == "__main__":
    main()
