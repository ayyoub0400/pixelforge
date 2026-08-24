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


  backend "s3" {

    bucket       = "pixelforge-tfstate-266735805454"
    key          = "pixelforge/dev/terraform.tfstate"
    region       = "eu-west-2"
    use_lockfile = true
    encrypt      = true

  }
}
