import json
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class Action:
    inject_type: str
    value: str

@dataclass
class Extended_Attributes:
    lookup_by_column: str
    output_states: str

@dataclass
class Transitions:
    transitions: Dict[str, str]

@dataclass
class Node:
    node_id: str
    node_type: str
    expected_text: str
    expected_silence: float
    timeout: int
    action_to_take: Optional[Action]
    transitions: Dict[str, str]
    language_ids: str
    persona: str
    minor_threshold_time: float
    major_threshold_time: float
    minor_confidence_level: float
    major_confidence_level: float
    extended_attributes: Optional[Extended_Attributes]

@dataclass
class Meta:
    test_execution_row_id: int
    phone_to_dial: str
    execution_mode: int
    compiled_at: str

@dataclass
class TestModelSettings:
    language_ids: str
    persona: str
    minor_threshold_time: float
    major_threshold_time: float
    minor_confidence_level: float
    major_confidence_level: float
    extended_attributes: Optional[Extended_Attributes]

class IVRTestCase:

    def __init__(self, json_data):

        self.meta = Meta(**json_data["meta"])

        # Test Model Settings

        # extended_attributes = None

        # if json_data["dial_plan"].get("extended_attributes"):

        #     extended_attributes = Extended_Attributes(
        #         lookup_by_column=json_data["dial_plan"]["extended_attributes"]["lookup_by_column"],
        #         output_states=json_data["dial_plan"]["extended_attributes"]["output_states"]
        #     )

        # test_model_settings = TestModelSettings(
        #     language_ids=json_data["dial_plan"]["language_ids"],
        #     persona=json_data["dial_plan"]["persona"],
        #     minor_threshold_time=json_data["dial_plan"]["minor_threshold_time"],
        #     major_threshold_time=json_data["dial_plan"]["major_threshold_time"],
        #     minor_confidence_level=json_data["dial_plan"]["minor_confidence_level"],
        #     major_confidence_level=json_data["dial_plan"]["major_confidence_level"],
        #     extended_attributes=extended_attributes
        # )

        # self.test_model_settings = test_model_settings

        # Data Nodes

        self.nodes = {}

        for node_id, node_data in json_data["dial_plan"].items():

            action = None

            if node_data.get("action_to_take"):

                action = Action(
                    inject_type=node_data["action_to_take"]["inject_type"],
                    value=node_data["action_to_take"]["value"]
                )

            extended_attributes = None

            if node_data.get("extended_attributes"):

                extended_attributes = Extended_Attributes(
                    lookup_by_column=node_data["extended_attributes"]["lookup_by_column"],
                    output_states=node_data["extended_attributes"]["output_states"]
                )


            node = Node(
                node_id=node_id,
                node_type=node_data["node_type"],
                expected_text=node_data["expected_text"],
                expected_silence=node_data["expected_silence"],
                timeout=node_data["timeout"],
                action_to_take=action,
                language_ids=node_data["language_ids"],
                persona=node_data["persona"],
                minor_threshold_time=node_data["minor_threshold_time"],
                major_threshold_time=node_data["major_threshold_time"],
                minor_confidence_level=node_data["minor_confidence_level"],
                major_confidence_level=node_data["major_confidence_level"],
                extended_attributes=extended_attributes,
                transitions=node_data.get("transitions", {})

            )

            self.nodes[node_id] = node

    def get_node(self, node_id):

        return self.nodes.get(node_id)

    def get_start_node(self):

        node_ids = sorted(self.nodes.keys())

        return self.nodes[node_ids[0]] if node_ids else None



def main():

    print("Opening ivr_test.json file")
    with open("ivr_test.json") as f:

        print("Loading data from ivr_test.json")

        data = json.load(f)

        test_case = IVRTestCase(data)

        print("PHONE NUMBER: ", test_case.meta.phone_to_dial)
        print("_" * 100)
        # print("Language IDs:", test_case.test_model_settings.language_ids)
        # print("Persona:", test_case.test_model_settings.persona)
        # print("Minor Threshold Time:", test_case.test_model_settings.minor_threshold_time)
        # print("Major Threshold Time:", test_case.test_model_settings.major_threshold_time)
        # print("Minor Confidence Level:", test_case.test_model_settings.minor_confidence_level)
        # print("Major Confidence Level:", test_case.test_model_settings.major_confidence_level)

        # if test_case.test_model_settings.extended_attributes:
        #     print("Extended Attributes:", test_case.test_model_settings.extended_attributes)

        # node = test_case.get_node("node_102")

        # print(node.node_type)
        # print(node.expected_text)

        # if node.action_to_take:
        #     print(node.action_to_take.inject_type)
        #     print(node.action_to_take.value)

        # print(node.transitions)


    current_node = test_case.get_start_node()

    while current_node:

        print("_" * 100)

        print(f"NODE ID: {current_node.node_id}: ")
        print(f"EXPECTED TEXT: {current_node.expected_text}")

        if current_node.action_to_take:
            print("INJECT TYPE: ", current_node.action_to_take.inject_type)
            print("ACTION DATA: ", current_node.action_to_take.value)

        print("Language IDs:", current_node.language_ids)
        print("Persona:", current_node.persona)

        try:
            if current_node.persona:
                for language_code in current_node.persona:
                    if current_node.persona[language_code]["VI"]:
                        print("Persona: ", language_code, " VoiceID ", current_node.persona[language_code]["VI"])
        except:
            pass

        print("Minor Threshold Time:", current_node.minor_threshold_time)
        print("Major Threshold Time:", current_node.major_threshold_time)
        print("Minor Confidence Level:", current_node.minor_confidence_level)
        print("Major Confidence Level:", current_node.major_confidence_level)

        if current_node.extended_attributes:
            print("Extended Attributes:", current_node.extended_attributes)

        print("TRANSITIONS: ",current_node.transitions)
   
        next_node_id = current_node.transitions.get(
            "on_success"
        )

        if not next_node_id:
            break

        current_node = test_case.get_node(
            next_node_id
        )

if __name__ == "__main__":
    main()