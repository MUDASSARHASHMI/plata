from datetime import datetime
from decimal import Decimal
from hashlib import sha1

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render_to_response
from django.template import RequestContext
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt

from plata.payment.modules.base import ProcessorBase
from plata.product.stock.models import StockTransaction


csrf_exempt_m = method_decorator(csrf_exempt)


# Copied from http://e-payment.postfinance.ch/ncol/paymentinfos1.asp
STATUSES = """\
0	Incomplete or invalid
1	Cancelled by client
2	Authorization refused
4	Order stored
41	Waiting client payment
5	Authorized
51	Authorization waiting
52	Authorization not known
55	Stand-by
59	Authoriz. to get manually
6	Authorized and cancelled
61	Author. deletion waiting
62	Author. deletion uncertain
63	Author. deletion refused
64	Authorized and cancelled
7	Payment deleted
71	Payment deletion pending
72	Payment deletion uncertain
73	Payment deletion refused
74	Payment deleted
75	Deletion processed by merchant
8	Refund
81	Refund pending
82	Refund uncertain
83	Refund refused
84	Payment declined by the acquirer
85	Refund processed by merchant
9	Payment requested
91	Payment processing
92	Payment uncertain
93	Payment refused
94	Refund declined by the acquirer
95	Payment processed by merchant
99	Being processed"""

STATUS_DICT = dict(line.split('\t') for line in STATUSES.splitlines())


class PaymentProcessor(ProcessorBase):
    name = _('Postfinance')

    def get_urls(self):
        from django.conf.urls.defaults import patterns, url

        return patterns('',
            url(r'^payment/postfinance/ipn/$', self.ipn, name='plata_payment_postfinance_ipn'),
            )

    def process_order_confirmed(self, request, order):
        POSTFINANCE = settings.POSTFINANCE

        if order.is_paid():
            return redirect('plata_order_already_paid')

        payment = order.payments.create(
            currency=order.currency,
            amount=order.balance_remaining,
            payment_module=u'%s' % self.name,
            )

        StockTransaction.objects.bulk_create(order,
            type=StockTransaction.PAYMENT_PROCESS_RESERVATION,
            notes=_('%(type)s transaction. %(order)s processed by %(payment_module)s') % {
                'type': _('payment process reservation'),
                'order': order,
                'payment_module': self.name,
                },
            negative=True,
            payment=payment)

        form_params = {
            'orderID': 'Order-%d-%d' % (order.id, payment.id),
            'amount': u'%s' % int(order.balance_remaining.quantize(Decimal('0.00'))*100),
            'currency': order.currency,
            'PSPID': POSTFINANCE['PSPID'],
            'mode': POSTFINANCE['LIVE'] and 'prod' or 'test',
            }

        form_params['SHASign'] = sha1(u''.join((
            form_params['orderID'],
            form_params['amount'],
            form_params['currency'],
            form_params['PSPID'],
            POSTFINANCE['SHA1_IN'],
            ))).hexdigest()

        return render_to_response('payment/postfinance_form.html', {
            'order': order,
            'HTTP_HOST': request.META.get('HTTP_HOST'),
            'form_params': form_params,
            }, context_instance=RequestContext(request))

    @csrf_exempt_m
    def ipn(self, request):
        POSTFINANCE = settings.POSTFINANCE

        try:
            try:
                orderID = request.POST['orderID']
                currency = request.POST['currency']
                amount = request.POST['amount']
                PM = request.POST['PM']
                ACCEPTANCE = request.POST['ACCEPTANCE']
                STATUS = request.POST['STATUS']
                CARDNO = request.POST['CARDNO']
                PAYID = request.POST['PAYID']
                NCERROR = request.POST['NCERROR']
                BRAND = request.POST['BRAND']
                SHASIGN = request.POST['SHASIGN']
            except KeyError:
                return HttpResponseForbidden('Missing data')

            sha1_source = u''.join((
                orderID,
                currency,
                amount,
                PM,
                ACCEPTANCE,
                STATUS,
                CARDNO,
                PAYID,
                NCERROR,
                BRAND,
                POSTFINANCE['SHA1_OUT'],
                ))

            sha1_out = sha1(sha1_source).hexdigest()

            if sha1_out.lower() != SHASIGN.lower():
                return HttpResponseForbidden('Hash did not validate')

            try:
                order, order_id, payment_id = orderID.split('-')
            except ValueError:
                return HttpResponseForbidden('Malformed order ID')

            order = get_object_or_404(self.shop.order_model, pk=order_id)
            try:
                payment = order.payments.get(pk=payment_id)
            except order.payments.model.DoesNotExist:
                payment = order.payments.model(
                    order=order,
                    payment_module=u'%s' % self.name,
                    )

            payment.currency = currency
            payment.amount = Decimal(amount)
            payment.data_json = request.POST.copy()
            payment.transaction_id = PAYID
            payment.payment_method = BRAND
            payment.notes = STATUS_DICT.get(STATUS)

            if STATUS == '5':
                payment.authorized = datetime.now()

            payment.save()

            StockTransaction.objects.bulk_create(order,
                type=StockTransaction.PAYMENT_PROCESS_RESERVATION,
                notes=_('%(type)s transaction. %(order)s processed by %(payment_module)s') % {
                'type': _('payment process reservation release'),
                    'order': order,
                    'payment_module': self.name,
                    },
                negative=False,
                payment=payment)

            if payment.authorized:
                StockTransaction.objects.bulk_create(order,
                    type=StockTransaction.SALE,
                    notes=_('%(type)s transaction. %(order)s processed by %(payment_module)s') % {
                        'type': _('sale'),
                        'order': order,
                        'payment_module': self.name,
                        },
                    negative=True,
                    payment=payment)

            return HttpResponse('OK')
        except Exception, e:
            import sys
            sys.stderr.write(unicode(e))
            sys.stderr.flush()
            return HttpResponseForbidden()