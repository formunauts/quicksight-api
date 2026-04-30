#!/bin/bash
ACCOUNT_ID="395443580020"

# 1. Get list of inactive users
USERS=$(aws quicksight list-users --aws-account-id $ACCOUNT_ID --namespace default \
  --query 'UserList[?Active==`false`].UserName' --output text)

if [ -z "$USERS" ]; then
    echo "No inactive users found."
    exit 0
fi

# 2. Loop and delete
for USER in $USERS; do
    echo "Deleting inactive user: $USER"
    aws quicksight delete-user --user-name "$USER" --aws-account-id $ACCOUNT_ID --namespace default
done

echo "Cleanup complete."