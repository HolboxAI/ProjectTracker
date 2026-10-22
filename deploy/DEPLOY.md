# Hosting Covex-Board on AWS

One EC2 instance, one Elastic IP, one SQLite file, `tracker.holbox.ai`, sign-in
required. This is the shape the app demands, not a shortcut: it is a single
long-running Python process over a local SQLite file in WAL mode, so anything
with more than one worker corrupts the database, and anything with an
ephemeral filesystem loses it.

## Why not the other AWS options

| Option | Why not |
| --- | --- |
| Lambda / API Gateway | No handler, and the filesystem is ephemeral — the SQLite file would be gone between requests |
| App Runner, autoscaled ECS | More than one process over the same SQLite file |
| ECS Fargate + EFS | WAL needs shared memory/mmap, which EFS does not provide; the journal mode would have to change first |
| **EC2 + EBS, one instance** | **Runs the app unmodified, on a real disk** |

## Shape

```
Route53  tracker.holbox.ai  ->  A  ->  Elastic IP
                                        |
                                   EC2 (Ubuntu 24.04, t4g.small)
                                        |
                                   nginx  :443   TLS via Let's Encrypt
                                        |
                                   python3 server.py  127.0.0.1:8077
                                        |
                                   /var/lib/covex-tracker/tracker.sqlite3   (EBS)
```

`t4g.small` with a 20 GB gp3 volume is roughly **$14/month** on-demand, less on
a savings plan. There is no load balancer: one instance means one address, and
an ALB would cost more than the server while adding a second place to look.

## What has to happen first

1. **Refresh the AWS session.** The `holbox-tracker` profile's token is
   expired, and re-authenticating opens a browser, so this has to be you:

   ```sh
   aws sso login --profile holbox-tracker
   aws sts get-caller-identity --profile holbox-tracker
   ```

2. **Confirm the DNS.** `tracker.holbox.ai` has to be a record in a Route53
   zone this account already hosts — `holbox.ai` is not in this project, and
   neither is the zone's ID.

3. **Confirm who gets in.** Sign-in is local accounts only for now, so access
   is "whoever has an account". Google Workspace SSO is the next step.

## Provision

Swap in the real zone ID and a key pair name you already have.

```sh
export AWS_PROFILE=holbox-tracker
export AWS_REGION=us-west-2
export ZONE_ID=ZXXXXXXXXXXXXX          # route53 zone for holbox.ai
export KEY_NAME=your-existing-keypair

# Ubuntu 24.04 LTS (arm64), resolved rather than hardcoded so it does not go stale.
AMI_ID=$(aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)

# Security group: 443 and 80 to the world, SSH to you alone.
SG_ID=$(aws ec2 create-security-group \
  --group-name covex-tracker --description "Covex-Board" \
  --query GroupId --output text)
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP/32"
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 80 --cidr 0.0.0.0/0

# The instance. IMDSv2 required; no public key beyond the one you name.
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" --instance-type t4g.small \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}' \
  --metadata-options 'HttpTokens=required' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=covex-tracker}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# An Elastic IP, so the address survives a stop/start.
ALLOC_ID=$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID"
ELASTIC_IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" \
  --query 'Addresses[0].PublicIp' --output text)
echo "Elastic IP: $ELASTIC_IP"

# DNS, pointing the name at the address.
aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --change-batch "{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{
    \"Name\":\"tracker.holbox.ai\",\"Type\":\"A\",\"TTL\":300,
    \"ResourceRecords\":[{\"Value\":\"$ELASTIC_IP\"}]}}]}"
```

## Deploy the code

```sh
# From this project directory.
scp -r server.py tracker static deploy ubuntu@$ELASTIC_IP:/tmp/covex-deploy
ssh -t ubuntu@$ELASTIC_IP \
  "sudo bash /tmp/covex-deploy/deploy/bootstrap.sh tracker.holbox.ai you@holbox.com"
```

Then, from then on:

```sh
sh deploy/redeploy.sh ubuntu@$ELASTIC_IP
```

`bootstrap.sh` installs Python and nginx, creates the `covex` service account,
copies the code to `/opt/covex-tracker`, generates the first administrator
password (left in `/root/covex-admin-password`, mode 600), starts the
`covex-tracker` systemd unit, and gets a Let's Encrypt certificate.

## Accounts

The app has a sign-in screen now; nothing is reachable without a session.

```sh
# On the instance.
sudo cat /root/covex-admin-password          # the first administrator, once

# From a checkout, against the server's database over SSH:
ssh ubuntu@$ELASTIC_IP

sudo -u covex python3 /opt/covex-tracker/server.py \
  --db /var/lib/covex-tracker/tracker.sqlite3 \
  --add-user --name "Jane Doe" --username jane --email jane@holbox.com
```

An administrator can also create accounts from **Manage accounts** in the app,
which returns a generated password exactly once — copy it then, because it
cannot be read back. `--list-users` and `--reset-password <username>` round out
the CLI.

A password is stored as a PBKDF2-SHA256 digest with a per-user salt, never as
text. A session is a random token in an `HttpOnly`, `SameSite=Lax`, `Secure`
cookie; only its SHA-256 is stored, so a stolen database backup does not hand
over live sessions. Eight failed attempts lock an account for fifteen minutes.

## Google Workspace SSO next

The users table already carries `provider`, `google_sub` and `email`, so
turning it on needs no migration and no second password for anyone:

1. Create an OAuth client in Google Cloud Console for the workspace, with
   `https://tracker.holbox.ai/auth/google/callback` as the redirect URI.
2. Add the flow to `server.py`: `/auth/google` redirects to Google with a
   `state` value kept in a short-lived cookie, `/auth/google/callback`
   exchanges the code, checks `hd=holbox.com`, and matches `email` against
   `users.email` (filling `google_sub`) — then calls the same
   `auth.start_session` that password sign-in uses.
3. Keep password sign-in for the administrator account as the way back in when
   the identity provider is unavailable.

The credentials in the local `.env` are the right shape for this step; nothing
reads them yet, and they should live in the instance's systemd environment
rather than the repository.

## Backup

Everything worth keeping is one file:

```sh
# On the instance, taken safely while the app is running.
sqlite3 /var/lib/covex-tracker/tracker.sqlite3 \
  ".backup /var/lib/covex-tracker/backup-$(date +%F).sqlite3"

# Or off the box, which is the point:
ssh ubuntu@$ELASTIC_IP \
  "sqlite3 /var/lib/covex-tracker/tracker.sqlite3 '.backup /tmp/b.sqlite3'" &&
  scp ubuntu@$ELASTIC_IP:/tmp/b.sqlite3 ./backup-$(date +%F).sqlite3
```

`.backup` is the right way to copy a live SQLite database; `cp` on a file in
WAL mode can catch it mid-write. A daily cron running the first form, with the
volume snapshotted weekly, is enough for a tool this size.

## Moving the existing data

The two databases hold different halves of what you want, and this is worth
being careful about:

| Database | Projects | Accounts |
| --- | --- | --- |
| `tracker.sqlite3` on the laptop | 4 — MOB, PHX, SCRIBING, STF, 25 issues | **none: it has no `users` table at all** |
| `/var/lib/covex-tracker/tracker.sqlite3` on the instance | none | the administrator you sign in with |

So copying either file over the other loses half of it, and replacing the
instance's database with the laptop's would leave a deployment holding four
projects and **nobody able to sign in** — the account exists only in the file
you just overwrote, and nothing below `server.py` can create one without it.
Merge instead:

```sh
# A consistent copy of the laptop database. Not `cp`: it is in WAL mode, and a
# plain copy can land between the database and its log.
python3 -c "
import sqlite3
s = sqlite3.connect('file:tracker.sqlite3?mode=ro', uri=True)
d = sqlite3.connect('/tmp/laptop.sqlite3'); s.backup(d); d.commit()"

scp /tmp/laptop.sqlite3 ubuntu@$ELASTIC_IP:/tmp/
ssh ubuntu@$ELASTIC_IP

# On the instance. Rehearse first -- --dry-run writes nothing.
sudo -u covex python3 /opt/covex-tracker/deploy/merge_projects.py \
  --source /tmp/laptop.sqlite3 \
  --target /var/lib/covex-tracker/tracker.sqlite3 --dry-run

sudo systemctl stop covex-tracker
sudo -u covex python3 /opt/covex-tracker/deploy/merge_projects.py \
  --source /tmp/laptop.sqlite3 \
  --target /var/lib/covex-tracker/tracker.sqlite3
sudo systemctl start covex-tracker
```

It inserts the projects and nothing else. `users`, `sessions` and
`login_attempts` in the target are never read and never written, so the
administrator you sign in with still works afterwards; sprint references,
comment threads and activity trails are remapped to follow their project; and
it refuses to run a second time rather than duplicating a project. Before it
writes, it leaves a consistent, timestamped backup of the target in `/tmp`.

Stop the service first. SQLite would cope with the writes arriving underneath a
running server, but there is no reason to make it — and the restart afterwards
is needed anyway.

Check what the instance actually holds rather than assuming it is still empty:
if people have been using it, it will have projects of its own, and a merge adds
to them rather than replacing them. The only thing that stops it is a project
*key* that exists on both sides, which is a refusal, not a silent overwrite.

The flags that matter are `--dry-run`, `--allow-existing` (only for two
projects that genuinely share a key) and the source being opened read-only, so
a mistake cannot damage the database you are copying *from*.

Do not delete the database to "start over": the service runs with `--no-seed`,
so a deleted file comes back as an empty schema, not a seeded workspace.
