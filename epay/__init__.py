import os
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from getpaid.backends import PaymentProcessorBase
from getpaid.utils import build_absolute_uri, get_domain, get_backend_settings

from .kkb.processing import Epay

__version__ = '0.1.2'


DEFAULT_KKB_PUB_KEY = os.path.join(os.path.dirname(__file__), "keys", "kkbca.pem")
DEFAULT_MERCHANT_PRIVATE_KEY = os.path.join(os.path.dirname(__file__), "keys", "cert.pem")


def same_id(id):
    return id


class PaymentProcessor(PaymentProcessorBase):
    BACKEND = 'epay'
    BACKEND_NAME = _('Kazkom Epay')

    BACKEND_ACCEPTED_CURRENCY_DICT = {
        'KZT': 398,
        'USD': 840,
        'EUR': 978,
    }
    BACKEND_ACCEPTED_CURRENCY = (u'KZT', u'USD', u'EUR')

    BACKEND_LOGO_URL = os.path.join(settings.STATIC_URL, 'epay/img/logo.png')

    backend_settings = get_backend_settings(BACKEND)
    epay = Epay(
        private_key=open(backend_settings.get(
            'merchant_private_key', DEFAULT_MERCHANT_PRIVATE_KEY), 'rb').read(),
        kkb_key=open(backend_settings.get(
            'kkb_pub_key', DEFAULT_KKB_PUB_KEY), 'rb').read(),
        **backend_settings
    )

    def get_scheme(self, request):
        scheme = self.get_backend_setting('scheme', None)
        if not scheme and request.is_secure():
            scheme = 'https'
        else:
            scheme = 'http'
        return scheme

    def get_language(self, request):
        """
        :return: rus|eng depending on user request
        """
        return "rus"

    def build_attrs(self, scheme='https', language="rus"):
        return {
            'Signed_Order_B64': self.epay.sign_order(self.payment.id, self.payment.amount, self.payment.currency),
            'Language': language,
            'BackLink': self.get_backlink_url(scheme=scheme),
            'PostLink': self.get_postlink_url(scheme=scheme)
        }

    def get_gateway_url(self, request):
        return (
            self.epay.get_gateway_url(),
            "POST",
            self.build_attrs(self.get_scheme(request), language=self.get_language(request))
        )

    def get_backlink_url(self, scheme):
        return "{}://{}{}".format(
            scheme,
            get_domain(),
            self.payment.order.get_absolute_url())

    def get_postlink_url(self, **kw):
        return build_absolute_uri('epay:postlink', **kw)

    @staticmethod
    def amount_to_python(amount_str):
        return Decimal(amount_str)

    @staticmethod
    def callback(response):
        """
        Payment was confirmed.
        """
        epay = PaymentProcessor.epay
        Payment = apps.get_model('getpaid', 'Payment')
        EpayPayment = apps.get_model('epay', 'EpayPayment')
        with transaction.atomic():
            params = epay.handle_response(response)
            customer_params = params['customer']
            payment_params = params['results']['payment']
            order_params = customer_params['merchant']['order']
            payment_id = epay.unmodify_order_id(int(order_params['order_id']))

            payment = Payment.objects.get(id=payment_id)
            assert payment.status == 'in_progress',\
                "Can not confirm payment that was not accepted for processing"
            payment.external_id = payment_params['reference']
            amount = PaymentProcessor.amount_to_python(payment_params['amount'])
            payment.amount_paid = amount

            epay_payment = EpayPayment(
                payment=payment,
                merchant_id=payment_params.get('merchant_id', ''),
                card=payment_params.get('card', ''),
                reference=payment_params.get('reference', ''),
                approval_code=payment_params.get('approval_code', ''),
                response_code=payment_params.get('response_code', ''),
                is_secure=(payment_params.get('secure', 'No') == 'Yes'),
                card_bin=payment_params.get('card_bin', ''),
                c_hash=payment_params.get('c_hash', ''),
                customer_name=customer_params.get('name', ''),
                customer_mail=customer_params.get('mail', ''),
                customer_phone=customer_params.get('phone', ''),
            )
            epay_payment.save()
            return payment.on_success(amount)

    @staticmethod
    def completed(payment_id):
        """
        Confirming payment. Money is transfered from card to our account
        """
        epay = PaymentProcessor.epay
        EpayPayment = apps.get_model('epay', 'EpayPayment')
        with transaction.atomic():
            epay_payment = EpayPayment.objects.select_related('payment').get(payment_id=payment_id)
            payment = epay_payment.payment
            epay.capture(
                payment_id, payment.amount, epay_payment.approval_code,
                epay_payment.reference, currency=payment.currency)

            # epay_payment.change_status("caputred")
            epay_payment.update_from_kkb()
            return epay_payment

    @staticmethod
    def reversed(payment_id):
        """
        Reversing money if they are still in block
        """
        EpayPayment = apps.get_model('epay', 'EpayPayment')
        epay = PaymentProcessor.epay
        with transaction.atomic():
            epay_payment = EpayPayment.objects.select_related('payment').get(payment_id=payment_id)
            payment = epay_payment.payment
            epay.cancel(
                payment_id, payment.amount, epay_payment.approval_code,
                epay_payment.reference, currency=payment.currency)

            payment.change_status("cancelled")

            return epay_payment

    @staticmethod
    def refunded(payment_id):
        """
        Reversing money if they are still in block
        """
        EpayPayment = apps.get_model('epay', 'EpayPayment')
        epay = PaymentProcessor.epay
        with transaction.atomic():
            epay_payment = EpayPayment.objects.select_related('payment').get(payment_id=payment_id)
            payment = epay_payment.payment
            epay.refund(
                payment_id, payment.amount, epay_payment.approval_code,
                epay_payment.reference, currency=payment.currency)

            payment.change_status("cancelled")

            return epay_payment

    @staticmethod
    def get_status(payment_id):
        epay = PaymentProcessor.epay
        response = epay.get_status(payment_id)
        return response['response']

    @staticmethod
    def update_status(payment_id):
        """
        Reversing money if they are still in block
        """
        EpayPayment = apps.get_model('epay', 'EpayPayment')
        with transaction.atomic():
            response = PaymentProcessor.get_status(payment_id)
            epay_payment = EpayPayment.import_or_update(response, payment_id=payment_id)
            return epay_payment
