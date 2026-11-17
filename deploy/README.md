# Deploying the chatbot on EC2

Credentials are **not** copied into the image. The container uses the EC2
instance's IAM role; boto3 discovers it automatically via the instance
metadata service (IMDS).

## 1. IAM role for the instance

1. Edit `bedrock-policy.json` and replace `<ACCOUNT_ID>` with your account ID.
2. Create the policy and a role that EC2 can assume, then an instance profile:

```bash
aws iam create-policy --policy-name ChatbotBedrockInvoke \
    --policy-document file://deploy/bedrock-policy.json

cat > trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

aws iam create-role --role-name ChatbotEc2Role --assume-role-policy-document file://trust.json
aws iam attach-role-policy --role-name ChatbotEc2Role \
    --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/ChatbotBedrockInvoke
aws iam create-instance-profile --instance-profile-name ChatbotEc2Profile
aws iam add-role-to-instance-profile --instance-profile-name ChatbotEc2Profile --role-name ChatbotEc2Role
```

3. Attach the profile to the instance (Console: Actions > Security > Modify IAM role), or:

```bash
aws ec2 associate-iam-instance-profile --instance-id <INSTANCE_ID> \
    --iam-instance-profile Name=ChatbotEc2Profile
```

## 2. Allow containers to reach IMDS (important)

IMDSv2 defaults to a hop limit of 1, which blocks containers on Docker's bridge
network ("Unable to locate credentials"). Raise it to 2:

```bash
aws ec2 modify-instance-metadata-options --instance-id <INSTANCE_ID> \
    --http-put-response-hop-limit 2 --http-tokens required --http-endpoint enabled
```

## 3. Get the image onto the instance

Option A - ECR (recommended):

```bash
aws ecr create-repository --repository-name chatbot
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker build -t chatbot .
docker tag chatbot:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/chatbot:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/chatbot:latest
```

The instance role also needs ECR pull permissions
(`AmazonEC2ContainerRegistryReadOnly`). On the instance, log in to ECR the same
way, then `docker pull`.

Option B - copy a tarball: `docker save chatbot:latest | gzip > chatbot.tar.gz`,
`scp` it over, then `docker load < chatbot.tar.gz`.

## 4. Run it on the instance

```bash
docker run -d --name chatbot --restart unless-stopped \
    -p 80:8000 -v chatbot-data:/data \
    -e AWS_REGION=us-east-1 \
    <IMAGE>
```

Open port 80 (or whichever you publish) in the instance's security group.
Chat history lives in the `chatbot-data` volume, so it survives restarts and
image upgrades.

## Local testing with your own credentials

Don't bake them into the image. Pass them at run time instead:

```bash
docker run --rm -p 8000:8000 -v "$HOME/.aws:/home/appuser/.aws:ro" -e AWS_REGION=us-east-1 chatbot
```
