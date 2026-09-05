"""remitaudit - payer underpayment detection for independent medical practices.

Ingests X12 835 electronic remittance advice files, de-identifies them at the
door, and surfaces claim lines where the payer allowed less than it should have.
"""

__version__ = "0.1.0"
