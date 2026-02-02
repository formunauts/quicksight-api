import boto3
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# configuration
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')
VPC_CONN_ID = os.getenv('VPC_CONN_ID')

def audit_network_path(qs_client, ec2_client):
    print('Starting network route audit')

    try:
        response = qs_client.describe_vpc_connection(
            AwsAccountId=QS_ACCOUNT_ID,
            VPCConnectionId=VPC_CONN_ID
        )

        conn_details = response['VPCConnection']
        vpc_id = conn_details.get('VPCId')

        if 'NetworkInterfaces' not in conn_details:
            print('No network interfaces found for this VPC connection')
            return

        subnets = [ni['SubnetId'] for ni in conn_details['NetworkInterfaces']]

        print(f'VPC connection ID: {VPC_CONN_ID}')
        print(f'VPC ID: {vpc_id}')
        print(f'Associated subnets: {subnets}')

    except Exception as e:
        print(f'Error fetching VPC connection details: {e}')
        return

    try:
        rts = ec2_client.describe_route_tables(
            Filters=[{'Name': 'association.subnet-id', 'Values': subnets}]
        )

        if not rts['RouteTables']:
            print('No explicit subnet route tables found, checking main route table')
            rts = ec2_client.describe_route_tables(
                Filters=[
                    {'Name': 'vpc-id', 'Values': [vpc_id]},
                    {'Name': 'association.main', 'Values': ['true']}
                ]
            )

        found_peering_route = False

        for rt in rts['RouteTables']:
            print(f'Route table ID: {rt["RouteTableId"]}')

            associations = [
                a.get('SubnetId', 'Main') for a in rt.get('Associations', [])
            ]
            print(f'Associations: {associations}')

            for route in rt.get('Routes', []):
                dest = route.get('DestinationCidrBlock', 'N/A')

                target = None
                if 'GatewayId' in route:
                    target = route['GatewayId']
                elif 'NatGatewayId' in route:
                    target = route['NatGatewayId']
                elif 'VpcPeeringConnectionId' in route:
                    target = route['VpcPeeringConnectionId']
                elif 'TransitGatewayId' in route:
                    target = route['TransitGatewayId']

                state = route.get('State', 'active')
                target_display = target or 'Unknown'

                print(f'Destination {dest} -> Target {target_display} ({state})')

                if target and (target.startswith('pcx-') or target.startswith('tgw-')):
                    found_peering_route = True

        if found_peering_route:
            print('At least one peering or transit route was found')
        else:
            print('No peering or transit routes were found')

    except Exception as e:
        print(f'Error checking route tables: {e}')

if __name__ == '__main__':
    qs = boto3.client('quicksight', region_name=REGION)
    ec2 = boto3.client('ec2', region_name=REGION)

    audit_network_path(qs, ec2)
