'''
Script to verify details of a specified VPC peering connection.
'''
import boto3
import os
from dotenv import load_dotenv
load_dotenv()

# configuration
REGION = os.getenv('QS_AWS_REGION')
PEERING_ID = os.getenv('QS_VPC_PEERING_ID')
DATA_VPC_ID = os.getenv('QS_DATA_VPC_ID')

def check_peering_details():
    ec2 = boto3.client('ec2', region_name=REGION)

    print(f'Inspecting VPC peering connection {PEERING_ID}')

    try:
        response = ec2.describe_vpc_peering_connections(
            VpcPeeringConnectionIds=[PEERING_ID]
        )

        pcx = response['VpcPeeringConnections'][0]
        status = pcx['Status']['Code']

        req_vpc = pcx['RequesterVpcInfo']['VpcId']
        acc_vpc = pcx['AccepterVpcInfo']['VpcId']

        print(f'Status: {status}')
        print(f'Requester VPC: {req_vpc}')
        print(f'Accepter VPC: {acc_vpc}')

        if req_vpc == DATA_VPC_ID or acc_vpc == DATA_VPC_ID:
            print('This peering connection includes the data team VPC')
        else:
            print(f'This peering connection does not include VPC {DATA_VPC_ID}')

    except Exception as e:
        print(f'Error inspecting peering connection: {e}')

if __name__ == '__main__':
    check_peering_details()
