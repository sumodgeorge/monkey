import functools
import ipaddress
import itertools
import logging
from typing import List

from bson import json_util

from common.config_value_paths import (
    EXPLOITER_CLASSES_PATH,
    LOCAL_NETWORK_SCAN_PATH,
    PASSWORD_LIST_PATH,
    SUBNET_SCAN_LIST_PATH,
    USER_LIST_PATH,
)
from common.network.network_range import NetworkRange
from common.network.segmentation_utils import get_ip_in_src_and_not_in_dst
from monkey_island.cc.database import mongo
from monkey_island.cc.models import Monkey
from monkey_island.cc.services.config import ConfigService
from monkey_island.cc.services.configuration.utils import (
    get_config_network_segments_as_subnet_groups,
)
from monkey_island.cc.services.node import NodeService
from monkey_island.cc.services.reporting.issue_processing.exploit_processing.exploiter_descriptor_enum import (  # noqa: E501
    ExploiterDescriptorEnum,
)
from monkey_island.cc.services.reporting.issue_processing.exploit_processing.processors.cred_exploit import (  # noqa: E501
    CredentialType,
)
from monkey_island.cc.services.reporting.issue_processing.exploit_processing.processors.exploit import (  # noqa: E501
    ExploiterReportInfo,
)
from monkey_island.cc.services.reporting.pth_report import PTHReportService
from monkey_island.cc.services.reporting.report_exporter_manager import ReportExporterManager
from monkey_island.cc.services.reporting.report_generation_synchronisation import (
    safe_generate_regular_report,
)
from monkey_island.cc.services.utils.network_utils import get_subnets, local_ip_addresses

__author__ = "itay.mizeretz"

logger = logging.getLogger(__name__)


class ReportService:
    class DerivedIssueEnum:
        WEAK_PASSWORD = "weak_password"
        STOLEN_CREDS = "stolen_creds"
        ZEROLOGON_PASS_RESTORE_FAILED = "zerologon_pass_restore_failed"

    @staticmethod
    def get_first_monkey_time():
        return (
            mongo.db.telemetry.find({}, {"timestamp": 1})
            .sort([("$natural", 1)])
            .limit(1)[0]["timestamp"]
        )

    @staticmethod
    def get_last_monkey_dead_time():
        return (
            mongo.db.telemetry.find({}, {"timestamp": 1})
            .sort([("$natural", -1)])
            .limit(1)[0]["timestamp"]
        )

    @staticmethod
    def get_monkey_duration():
        delta = ReportService.get_last_monkey_dead_time() - ReportService.get_first_monkey_time()
        st = ""
        hours, rem = divmod(delta.seconds, 60 * 60)
        minutes, seconds = divmod(rem, 60)

        if delta.days > 0:
            st += "%d days, " % delta.days
        if hours > 0:
            st += "%d hours, " % hours
        st += "%d minutes and %d seconds" % (minutes, seconds)

        return st

    @staticmethod
    def get_tunnels():
        return [
            {
                "type": "tunnel",
                "machine": NodeService.get_node_hostname(
                    NodeService.get_node_or_monkey_by_id(tunnel["_id"])
                ),
                "dest": NodeService.get_node_hostname(
                    NodeService.get_node_or_monkey_by_id(tunnel["tunnel"])
                ),
            }
            for tunnel in mongo.db.monkey.find({"tunnel": {"$exists": True}}, {"tunnel": 1})
        ]

    @staticmethod
    def get_azure_issues():
        creds = ReportService.get_azure_creds()
        machines = set([instance["origin"] for instance in creds])

        logger.info("Azure issues generated for reporting")

        return [
            {
                "type": "azure_password",
                "machine": machine,
                "users": set(
                    [instance["username"] for instance in creds if instance["origin"] == machine]
                ),
            }
            for machine in machines
        ]

    @staticmethod
    def get_scanned():
        formatted_nodes = []

        nodes = ReportService.get_all_displayed_nodes()

        for node in nodes:
            nodes_that_can_access_current_node = node["accessible_from_nodes_hostnames"]
            formatted_nodes.append(
                {
                    "label": node["label"],
                    "ip_addresses": node["ip_addresses"],
                    "accessible_from_nodes": nodes_that_can_access_current_node,
                    "services": node["services"],
                    "domain_name": node["domain_name"],
                    "pba_results": node["pba_results"] if "pba_results" in node else "None",
                }
            )

        logger.info("Scanned nodes generated for reporting")

        return formatted_nodes

    @staticmethod
    def get_all_displayed_nodes():
        nodes_without_monkeys = [
            NodeService.get_displayed_node_by_id(node["_id"], True)
            for node in mongo.db.node.find({}, {"_id": 1})
        ]
        nodes_with_monkeys = [
            NodeService.get_displayed_node_by_id(monkey["_id"], True)
            for monkey in mongo.db.monkey.find({}, {"_id": 1})
        ]
        nodes = nodes_without_monkeys + nodes_with_monkeys
        return nodes

    @staticmethod
    def get_exploited():
        exploited_with_monkeys = [
            NodeService.get_displayed_node_by_id(monkey["_id"], True)
            for monkey in mongo.db.monkey.find({}, {"_id": 1})
            if not NodeService.get_monkey_manual_run(NodeService.get_monkey_by_id(monkey["_id"]))
        ]

        exploited_without_monkeys = [
            NodeService.get_displayed_node_by_id(node["_id"], True)
            for node in mongo.db.node.find({"exploited": True}, {"_id": 1})
        ]

        exploited = exploited_with_monkeys + exploited_without_monkeys

        exploited = [
            {
                "label": exploited_node["label"],
                "ip_addresses": exploited_node["ip_addresses"],
                "domain_name": exploited_node["domain_name"],
                "exploits": ReportService.get_exploits_used_on_node(exploited_node),
            }
            for exploited_node in exploited
        ]

        logger.info("Exploited nodes generated for reporting")

        return exploited

    @staticmethod
    def get_exploits_used_on_node(node: dict) -> List[str]:
        return list(
            set(
                [
                    ExploiterDescriptorEnum.get_by_class_name(exploit["exploiter"]).display_name
                    for exploit in node["exploits"]
                    if exploit["result"]
                ]
            )
        )

    @staticmethod
    def get_stolen_creds():
        creds = []

        stolen_system_info_creds = ReportService._get_credentials_from_system_info_telems()
        creds.extend(stolen_system_info_creds)

        stolen_exploit_creds = ReportService._get_credentials_from_exploit_telems()
        creds.extend(stolen_exploit_creds)

        logger.info("Stolen creds generated for reporting")
        return creds

    @staticmethod
    def _get_credentials_from_system_info_telems():
        formatted_creds = []
        for telem in mongo.db.telemetry.find(
            {"telem_category": "system_info", "data.credentials": {"$exists": True}},
            {"data.credentials": 1, "monkey_guid": 1},
        ):
            creds = telem["data"]["credentials"]
            origin = NodeService.get_monkey_by_guid(telem["monkey_guid"])["hostname"]
            formatted_creds.extend(ReportService._format_creds_for_reporting(telem, creds, origin))
        return formatted_creds

    @staticmethod
    def _get_credentials_from_exploit_telems():
        formatted_creds = []
        for telem in mongo.db.telemetry.find(
            {"telem_category": "exploit", "data.info.credentials": {"$exists": True}},
            {"data.info.credentials": 1, "data.machine": 1, "monkey_guid": 1},
        ):
            creds = telem["data"]["info"]["credentials"]
            domain_name = telem["data"]["machine"]["domain_name"]
            ip = telem["data"]["machine"]["ip_addr"]
            origin = domain_name if domain_name else ip
            formatted_creds.extend(ReportService._format_creds_for_reporting(telem, creds, origin))
        return formatted_creds

    @staticmethod
    def _format_creds_for_reporting(telem, monkey_creds, origin):
        creds = []
        CRED_TYPE_DICT = {
            "password": "Clear Password",
            "lm_hash": "LM hash",
            "ntlm_hash": "NTLM hash",
        }
        if len(monkey_creds) == 0:
            return []

        for user in monkey_creds:
            for cred_type in CRED_TYPE_DICT:
                if cred_type not in monkey_creds[user] or not monkey_creds[user][cred_type]:
                    continue
                username = (
                    monkey_creds[user]["username"] if "username" in monkey_creds[user] else user
                )
                cred_row = {
                    "username": username,
                    "type": CRED_TYPE_DICT[cred_type],
                    "origin": origin,
                }
                if cred_row not in creds:
                    creds.append(cred_row)
        return creds

    @staticmethod
    def get_ssh_keys():
        """
        Return private ssh keys found as credentials
        :return: List of credentials
        """
        creds = []
        for telem in mongo.db.telemetry.find(
            {"telem_category": "system_info", "data.ssh_info": {"$exists": True}},
            {"data.ssh_info": 1, "monkey_guid": 1},
        ):
            origin = NodeService.get_monkey_by_guid(telem["monkey_guid"])["hostname"]
            if telem["data"]["ssh_info"]:
                # Pick out all ssh keys not yet included in creds
                ssh_keys = [
                    {
                        "username": key_pair["name"],
                        "type": "Clear SSH private key",
                        "origin": origin,
                    }
                    for key_pair in telem["data"]["ssh_info"]
                    if key_pair["private_key"]
                    and {
                        "username": key_pair["name"],
                        "type": "Clear SSH private key",
                        "origin": origin,
                    }
                    not in creds
                ]
                creds.extend(ssh_keys)
        return creds

    @staticmethod
    def get_azure_creds():
        """
        Recover all credentials marked as being from an Azure machine
        :return: List of credentials.
        """
        creds = []
        for telem in mongo.db.telemetry.find(
            {"telem_category": "system_info", "data.Azure": {"$exists": True}},
            {"data.Azure": 1, "monkey_guid": 1},
        ):
            azure_users = telem["data"]["Azure"]["usernames"]
            if len(azure_users) == 0:
                continue
            origin = NodeService.get_monkey_by_guid(telem["monkey_guid"])["hostname"]
            azure_leaked_users = [
                {"username": user.replace(",", "."), "type": "Clear Password", "origin": origin}
                for user in azure_users
            ]
            creds.extend(azure_leaked_users)

        logger.info("Azure machines creds generated for reporting")
        return creds

    @staticmethod
    def process_exploit(exploit) -> ExploiterReportInfo:
        exploiter_type = exploit["data"]["exploiter"]
        exploiter_descriptor = ExploiterDescriptorEnum.get_by_class_name(exploiter_type)
        processor = exploiter_descriptor.processor()
        exploiter_info = processor.get_exploit_info_by_dict(exploiter_type, exploit)
        return exploiter_info

    @staticmethod
    def get_exploits() -> List[dict]:
        query = [
            {"$match": {"telem_category": "exploit", "data.result": True}},
            {
                "$group": {
                    "_id": {"ip_address": "$data.machine.ip_addr"},
                    "data": {"$first": "$$ROOT"},
                }
            },
            {"$replaceRoot": {"newRoot": "$data"}},
        ]
        exploits = []
        for exploit in mongo.db.telemetry.aggregate(query):
            new_exploit = ReportService.process_exploit(exploit)
            if new_exploit not in exploits:
                exploits.append(new_exploit.__dict__)
        return exploits

    @staticmethod
    def get_monkey_subnets(monkey_guid):
        network_info = mongo.db.telemetry.find_one(
            {"telem_category": "system_info", "monkey_guid": monkey_guid},
            {"data.network_info.networks": 1},
        )
        if network_info is None or not network_info["data"]:
            return []

        return [
            ipaddress.ip_interface(str(network["addr"] + "/" + network["netmask"])).network
            for network in network_info["data"]["network_info"]["networks"]
        ]

    @staticmethod
    def get_island_cross_segment_issues():
        issues = []
        island_ips = local_ip_addresses()
        for monkey in mongo.db.monkey.find(
            {"tunnel": {"$exists": False}}, {"tunnel": 1, "guid": 1, "hostname": 1}
        ):
            found_good_ip = False
            monkey_subnets = ReportService.get_monkey_subnets(monkey["guid"])
            for subnet in monkey_subnets:
                for ip in island_ips:
                    if ipaddress.ip_address(str(ip)) in subnet:
                        found_good_ip = True
                        break
                if found_good_ip:
                    break
            if not found_good_ip:
                issues.append(
                    {
                        "type": "island_cross_segment",
                        "machine": monkey["hostname"],
                        "networks": [str(subnet) for subnet in monkey_subnets],
                        "server_networks": [str(subnet) for subnet in get_subnets()],
                    }
                )

        return issues

    @staticmethod
    def get_cross_segment_issues_of_single_machine(source_subnet_range, target_subnet_range):
        """
        Gets list of cross segment issues of a single machine. Meaning a machine has an interface
        for each of the
        subnets.
        :param source_subnet_range:   The subnet range which shouldn't be able to access
        target_subnet.
        :param target_subnet_range:   The subnet range which shouldn't be accessible from
        source_subnet.
        :return:
        """
        cross_segment_issues = []

        for monkey in mongo.db.monkey.find({}, {"ip_addresses": 1, "hostname": 1}):
            ip_in_src = None
            ip_in_dst = None
            for ip_addr in monkey["ip_addresses"]:
                if source_subnet_range.is_in_range(str(ip_addr)):
                    ip_in_src = ip_addr
                    break

            # No point searching the dst subnet if there are no IPs in src subnet.
            if not ip_in_src:
                continue

            for ip_addr in monkey["ip_addresses"]:
                if target_subnet_range.is_in_range(str(ip_addr)):
                    ip_in_dst = ip_addr
                    break

            if ip_in_dst:
                cross_segment_issues.append(
                    {
                        "source": ip_in_src,
                        "hostname": monkey["hostname"],
                        "target": ip_in_dst,
                        "services": None,
                        "is_self": True,
                    }
                )

        return cross_segment_issues

    @staticmethod
    def get_cross_segment_issues_per_subnet_pair(scans, source_subnet, target_subnet):
        """
        Gets list of cross segment issues from source_subnet to target_subnet.
        :param scans:           List of all scan telemetry entries. Must have monkey_guid,
        ip_addr and services.
                                This should be a PyMongo cursor object.
        :param source_subnet:   The subnet which shouldn't be able to access target_subnet.
        :param target_subnet:   The subnet which shouldn't be accessible from source_subnet.
        :return:
        """
        if source_subnet == target_subnet:
            return []
        source_subnet_range = NetworkRange.get_range_obj(source_subnet)
        target_subnet_range = NetworkRange.get_range_obj(target_subnet)

        cross_segment_issues = []

        scans.rewind()  # If we iterated over scans already we need to rewind.
        for scan in scans:
            target_ip = scan["data"]["machine"]["ip_addr"]
            if target_subnet_range.is_in_range(str(target_ip)):
                monkey = NodeService.get_monkey_by_guid(scan["monkey_guid"])
                cross_segment_ip = get_ip_in_src_and_not_in_dst(
                    monkey["ip_addresses"], source_subnet_range, target_subnet_range
                )

                if cross_segment_ip is not None:
                    cross_segment_issues.append(
                        {
                            "source": cross_segment_ip,
                            "hostname": monkey["hostname"],
                            "target": target_ip,
                            "services": scan["data"]["machine"]["services"],
                            "icmp": scan["data"]["machine"]["icmp"],
                            "is_self": False,
                        }
                    )

        return cross_segment_issues + ReportService.get_cross_segment_issues_of_single_machine(
            source_subnet_range, target_subnet_range
        )

    @staticmethod
    def get_cross_segment_issues_per_subnet_group(scans, subnet_group):
        """
        Gets list of cross segment issues within given subnet_group.
        :param scans:           List of all scan telemetry entries. Must have monkey_guid,
        ip_addr and services.
                                This should be a PyMongo cursor object.
        :param subnet_group:    List of subnets which shouldn't be accessible from each other.
        :return:                Cross segment issues regarding the subnets in the group.
        """
        cross_segment_issues = []

        for subnet_pair in itertools.product(subnet_group, subnet_group):
            source_subnet = subnet_pair[0]
            target_subnet = subnet_pair[1]
            pair_issues = ReportService.get_cross_segment_issues_per_subnet_pair(
                scans, source_subnet, target_subnet
            )
            if len(pair_issues) != 0:
                cross_segment_issues.append(
                    {
                        "source_subnet": source_subnet,
                        "target_subnet": target_subnet,
                        "issues": pair_issues,
                    }
                )

        return cross_segment_issues

    @staticmethod
    def get_cross_segment_issues():
        scans = mongo.db.telemetry.find(
            {"telem_category": "scan"},
            {
                "monkey_guid": 1,
                "data.machine.ip_addr": 1,
                "data.machine.services": 1,
                "data.machine.icmp": 1,
            },
        )

        cross_segment_issues = []

        # For now the feature is limited to 1 group.
        subnet_groups = get_config_network_segments_as_subnet_groups()

        for subnet_group in subnet_groups:
            cross_segment_issues += ReportService.get_cross_segment_issues_per_subnet_group(
                scans, subnet_group
            )

        return cross_segment_issues

    @staticmethod
    def get_domain_issues():
        ISSUE_GENERATORS = [
            PTHReportService.get_duplicated_passwords_issues,
            PTHReportService.get_shared_admins_issues,
        ]
        issues = functools.reduce(lambda acc, issue_gen: acc + issue_gen(), ISSUE_GENERATORS, [])
        domain_issues_dict = {}
        for issue in issues:
            if not issue.get("is_local", True):
                machine = issue.get("machine").upper()
                aws_instance_id = ReportService.get_machine_aws_instance_id(issue.get("machine"))
                if machine not in domain_issues_dict:
                    domain_issues_dict[machine] = []
                if aws_instance_id:
                    issue["aws_instance_id"] = aws_instance_id
                domain_issues_dict[machine].append(issue)
        logger.info("Domain issues generated for reporting")
        return domain_issues_dict

    @staticmethod
    def get_machine_aws_instance_id(hostname):
        aws_instance_id_list = list(
            mongo.db.monkey.find({"hostname": hostname}, {"aws_instance_id": 1})
        )
        if aws_instance_id_list:
            if "aws_instance_id" in aws_instance_id_list[0]:
                return str(aws_instance_id_list[0]["aws_instance_id"])
        else:
            return None

    @staticmethod
    def get_manual_monkeys():
        return [
            monkey["hostname"]
            for monkey in mongo.db.monkey.find({}, {"hostname": 1, "parent": 1, "guid": 1})
            if NodeService.get_monkey_manual_run(monkey)
        ]

    @staticmethod
    def get_config_users():
        return ConfigService.get_config_value(USER_LIST_PATH, True, True)

    @staticmethod
    def get_config_passwords():
        return ConfigService.get_config_value(PASSWORD_LIST_PATH, True, True)

    @staticmethod
    def get_config_exploits():
        exploits_config_value = EXPLOITER_CLASSES_PATH
        default_exploits = ConfigService.get_default_config(False)
        for namespace in exploits_config_value:
            default_exploits = default_exploits[namespace]
        exploits = ConfigService.get_config_value(exploits_config_value, True, True)

        if exploits == default_exploits:
            return ["default"]

        return [
            ExploiterDescriptorEnum.get_by_class_name(exploit).display_name for exploit in exploits
        ]

    @staticmethod
    def get_config_ips():
        return ConfigService.get_config_value(SUBNET_SCAN_LIST_PATH, True, True)

    @staticmethod
    def get_config_scan():
        return ConfigService.get_config_value(LOCAL_NETWORK_SCAN_PATH, True, True)

    @staticmethod
    def get_issue_set(issues, config_users, config_passwords):
        issue_set = set()

        for machine in issues:
            for issue in issues[machine]:
                if ReportService._is_weak_credential_issue(issue, config_users, config_passwords):
                    issue_set.add(ReportService.DerivedIssueEnum.WEAK_PASSWORD)
                elif ReportService._is_stolen_credential_issue(issue):
                    issue_set.add(ReportService.DerivedIssueEnum.STOLEN_CREDS)
                elif ReportService._is_zerologon_pass_restore_failed(issue):
                    issue_set.add(ReportService.DerivedIssueEnum.ZEROLOGON_PASS_RESTORE_FAILED)

                issue_set.add(issue["type"])

        return issue_set

    @staticmethod
    def _is_weak_credential_issue(
        issue: dict, config_usernames: List[str], config_passwords: List[str]
    ) -> bool:
        # Only credential exploiter issues have 'credential_type'
        return (
            "credential_type" in issue
            and issue["credential_type"] == CredentialType.PASSWORD.value
            and issue["password"] in config_passwords
            and issue["username"] in config_usernames
        )

    @staticmethod
    def _is_stolen_credential_issue(issue: dict) -> bool:
        # Only credential exploiter issues have 'credential_type'
        return "credential_type" in issue and (
            issue["credential_type"] == CredentialType.PASSWORD.value
            or issue["credential_type"] == CredentialType.HASH.value
        )

    @staticmethod
    def _is_zerologon_pass_restore_failed(issue: dict):
        return (
            issue["type"] == ExploiterDescriptorEnum.ZEROLOGON.value.class_name
            and not issue["password_restored"]
        )

    @staticmethod
    def is_report_generated():
        generated_report = mongo.db.report.find_one({})
        return generated_report is not None

    @staticmethod
    def generate_report():
        domain_issues = ReportService.get_domain_issues()
        issues = ReportService.get_issues()
        config_users = ReportService.get_config_users()
        config_passwords = ReportService.get_config_passwords()
        issue_set = ReportService.get_issue_set(issues, config_users, config_passwords)
        cross_segment_issues = ReportService.get_cross_segment_issues()
        monkey_latest_modify_time = Monkey.get_latest_modifytime()

        scanned_nodes = ReportService.get_scanned()
        exploited_nodes = ReportService.get_exploited()
        report = {
            "overview": {
                "manual_monkeys": ReportService.get_manual_monkeys(),
                "config_users": config_users,
                "config_passwords": config_passwords,
                "config_exploits": ReportService.get_config_exploits(),
                "config_ips": ReportService.get_config_ips(),
                "config_scan": ReportService.get_config_scan(),
                "monkey_start_time": ReportService.get_first_monkey_time().strftime(
                    "%d/%m/%Y %H:%M:%S"
                ),
                "monkey_duration": ReportService.get_monkey_duration(),
                "issues": issue_set,
                "cross_segment_issues": cross_segment_issues,
            },
            "glance": {
                "scanned": scanned_nodes,
                "exploited": exploited_nodes,
                "stolen_creds": ReportService.get_stolen_creds(),
                "azure_passwords": ReportService.get_azure_creds(),
                "ssh_keys": ReportService.get_ssh_keys(),
                "strong_users": PTHReportService.get_strong_users_on_crit_details(),
            },
            "recommendations": {"issues": issues, "domain_issues": domain_issues},
            "meta": {"latest_monkey_modifytime": monkey_latest_modify_time},
        }
        ReportExporterManager().export(report)
        mongo.db.report.drop()
        mongo.db.report.insert_one(ReportService.encode_dot_char_before_mongo_insert(report))

        return report

    @staticmethod
    def get_issues():
        ISSUE_GENERATORS = [
            ReportService.get_exploits,
            ReportService.get_tunnels,
            ReportService.get_island_cross_segment_issues,
            ReportService.get_azure_issues,
            PTHReportService.get_duplicated_passwords_issues,
            PTHReportService.get_strong_users_on_crit_issues,
        ]

        issues = functools.reduce(lambda acc, issue_gen: acc + issue_gen(), ISSUE_GENERATORS, [])

        issues_dict = {}
        for issue in issues:
            if issue.get("is_local", True):
                machine = issue.get("machine").upper()
                aws_instance_id = ReportService.get_machine_aws_instance_id(issue.get("machine"))
                if machine not in issues_dict:
                    issues_dict[machine] = []
                if aws_instance_id:
                    issue["aws_instance_id"] = aws_instance_id
                issues_dict[machine].append(issue)
        logger.info("Issues generated for reporting")
        return issues_dict

    @staticmethod
    def encode_dot_char_before_mongo_insert(report_dict):
        """
        mongodb doesn't allow for '.' and '$' in a key's name, this function replaces the '.'
        char with the unicode
        ,,, combo instead.
        :return: dict with formatted keys with no dots.
        """
        report_as_json = json_util.dumps(report_dict).replace(".", ",,,")
        return json_util.loads(report_as_json)

    @staticmethod
    def is_latest_report_exists():
        """
        This function checks if a monkey report was already generated and if it's the latest one.
        :return: True if report is the latest one, False if there isn't a report or its not the
        latest.
        """
        latest_report_doc = mongo.db.report.find_one({}, {"meta.latest_monkey_modifytime": 1})

        if latest_report_doc:
            report_latest_modifytime = latest_report_doc["meta"]["latest_monkey_modifytime"]
            latest_monkey_modifytime = Monkey.get_latest_modifytime()
            return report_latest_modifytime == latest_monkey_modifytime

        return False

    @staticmethod
    def delete_saved_report_if_exists():
        """
        This function clears the saved report from the DB.
        :raises RuntimeError if deletion failed
        """
        delete_result = mongo.db.report.delete_many({})
        if mongo.db.report.count_documents({}) != 0:
            raise RuntimeError(
                "Report cache not cleared. DeleteResult: " + delete_result.raw_result
            )

    @staticmethod
    def decode_dot_char_before_mongo_insert(report_dict):
        """
        this function replaces the ',,,' combo with the '.' char instead.
        :return: report dict with formatted keys (',,,' -> '.')
        """
        report_as_json = json_util.dumps(report_dict).replace(",,,", ".")
        return json_util.loads(report_as_json)

    @staticmethod
    def get_report():
        if ReportService.is_latest_report_exists():
            return ReportService.decode_dot_char_before_mongo_insert(mongo.db.report.find_one())
        return safe_generate_regular_report()

    @staticmethod
    def did_exploit_type_succeed(exploit_type):
        return (
            mongo.db.edge.count(
                {"exploits": {"$elemMatch": {"exploiter": exploit_type, "result": True}}}, limit=1
            )
            > 0
        )
