output "state_buckets" {
  description = "Bucket names and locations used by the production roots."
  value = {
    for key, bucket in cloudflare_r2_bucket.terraform_state :
    key => {
      name     = bucket.name
      location = bucket.location
    }
  }
}

output "dockerswarm_backup_bucket" {
  description = "Dedicated private bucket consumed by the backup Ansible role."
  value = {
    name     = cloudflare_r2_bucket.dockerswarm_backups.name
    location = cloudflare_r2_bucket.dockerswarm_backups.location
  }
}
