#terraform and provider version contraints 
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source = "hashicorp/aws"

      #restrict to version 5.* 
      #prevent upgrades top version 6
      version = "~>5.0"
    }
  }
}
