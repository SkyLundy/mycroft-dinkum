Feature: mycroft-ip

  Scenario: last digits of IP
    Given an english speaking user
     When the user says "what's the last digits of your ip"
     Then "ip.mark2" should reply with dialog from "last digits.dialog"
