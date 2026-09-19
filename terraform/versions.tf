terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state is intentionally not hard-coded: it differs per organisation.
  # Configure it at init time, e.g.
  #   terraform init \
  #     -backend-config="bucket=my-tf-state" \
  #     -backend-config="key=fahem/terraform.tfstate" \
  #     -backend-config="region=eu-west-1" \
  #     -backend-config="use_lockfile=true"
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
