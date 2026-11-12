@auth @core @unauthenticated @qr-login
Feature: Visitor QR-code login for web apps

  # The QR login protocol is provider-agnostic: the "scanner" side (Bilibili,
  # where real users scan the QR with their account) is driven through the
  # signed provider callback API (HMAC-SHA256, platform SECRET_KEY) instead of
  # a real provider app. Openids are Bilibili-style UIDs. The fixture web app
  # is prepared out-of-band and referenced by E2E_QR_LOGIN_APP_CODE (a
  # web-auth-enabled web app whose visitor access rules admit anonymous QR
  # visitors).

  Scenario: Scan and confirm a QR ticket to sign into the web app
    Given a web app for QR login is available
    When I open the web app sign-in page
    And the QR login panel is showing a ticket
    When the QR ticket is scanned with provider openid "349118612"
    And the QR ticket is confirmed
    Then the visitor is signed in through the QR login panel

  Scenario: Refresh an expired QR ticket and scan the fresh one
    Given a web app for QR login is available
    When I open the web app sign-in page
    And the QR login panel is showing a ticket
    When I refresh the QR login ticket
    And the fresh QR ticket is scanned with provider openid "349118612"
    And the QR ticket is confirmed
    Then the visitor is signed in through the QR login panel

  Scenario: The same provider openid always rebinds the same visitor identity
    Given a web app for QR login is available
    When I open the web app sign-in page
    And the QR login panel is showing a ticket
    When the QR ticket is scanned with provider openid "349118612"
    And the QR ticket is confirmed
    Then the visitor is signed in through the QR login panel
    And the QR login identity for openid "349118612" is stable across logins

  Scenario: Forged provider callbacks are rejected
    Given a web app for QR login is available
    When I open the web app sign-in page
    And the QR login panel is showing a ticket
    When a forged QR ticket event is submitted
    Then the provider callback is rejected as invalid
