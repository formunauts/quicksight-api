import boto3
import sys
import os
from dotenv import load_dotenv
load_dotenv()

# configuration
REGION = os.getenv('QS_AWS_REGION')
CLUSTER_ID = os.getenv('CLUSTER_ID')

def check_firewall_deep():
    print('Inspecting RDS security groups')

    rds = boto3.client('rds', region_name=REGION)
    ec2 = boto3.client('ec2', region_name=REGION)

    try:
        response = rds.describe_db_clusters(DBClusterIdentifier=CLUSTER_ID)
        sg_list = response['DBClusters'][0]['VpcSecurityGroups']

        if not sg_list:
            print('No security groups attached to cluster')
            return

        sg_ids = [item['VpcSecurityGroupId'] for item in sg_list]
        print(f'Found security groups: {sg_ids}')

        sg_resp = ec2.describe_security_groups(GroupIds=sg_ids)

        found_prod_access = False

        for sg in sg_resp['SecurityGroups']:
            print(f'Checking security group {sg["GroupId"]} ({sg["GroupName"]})')

            for rule in sg.get('IpPermissions', []):
                from_port = rule.get('FromPort')
                to_port = rule.get('ToPort')
                protocol = rule.get('IpProtocol')

                is_postgres = (
                    protocol == '-1' or
                    (from_port == 5432 and to_port == 5432)
                )

                if not is_postgres:
                    continue

                for ip_range in rule.get('IpRanges', []):
                    cidr = ip_range.get('CidrIp')
                    print(f'Allowed CIDR: {cidr}')

                    if cidr and cidr.startswith('10.10.'):
                        found_prod_access = True

                for group in rule.get('UserIdGroupPairs', []):
                    print(f'Allowed security group: {group["GroupId"]}')

        if found_prod_access:
            print('Prod access found in at least one security group')
        else:
            print('No prod access found in attached security groups')

    except Exception as e:
        print(f'Error inspecting firewall: {e}')

if __name__ == '__main__':
    check_firewall_deep()