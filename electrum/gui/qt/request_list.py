#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from enum import IntEnum

from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtWidgets import QMenu, QHeaderView
from PyQt5.QtCore import Qt, QItemSelectionModel

from electrum.i18n import _
from electrum.util import format_time, age, get_request_status
from electrum.util import PR_TYPE_ADDRESS, PR_TYPE_LN, PR_TYPE_BIP70
from electrum.util import PR_UNPAID, PR_EXPIRED, PR_PAID, PR_UNKNOWN, PR_INFLIGHT, pr_tooltips
from electrum.lnutil import SENT, RECEIVED
from electrum.plugin import run_hook
from electrum.wallet import InternalAddressCorruption
from electrum.bitcoin import COIN
from electrum.lnaddr import lndecode
import electrum.constants as constants

from .util import MyTreeView, pr_icons, read_QIcon, webopen


ROLE_REQUEST_TYPE = Qt.UserRole
ROLE_KEY = Qt.UserRole + 1

class RequestList(MyTreeView):

    class Columns(IntEnum):
        DATE = 0
        DESCRIPTION = 1
        AMOUNT = 2
        STATUS = 3

    headers = {
        Columns.DATE: _('Date'),
        Columns.DESCRIPTION: _('Description'),
        Columns.AMOUNT: _('Amount'),
        Columns.STATUS: _('Status'),
    }
    filter_columns = [Columns.DATE, Columns.DESCRIPTION, Columns.AMOUNT]

    def __init__(self, parent):
        super().__init__(parent, self.create_menu,
                         stretch_column=self.Columns.DESCRIPTION,
                         editable_columns=[])
        self.setModel(QStandardItemModel(self))
        self.setSortingEnabled(True)
        self.update()
        self.selectionModel().currentRowChanged.connect(self.item_changed)

    def select_key(self, key):
        for i in range(self.model().rowCount()):
            item = self.model().index(i, self.Columns.DATE)
            row_key = item.data(ROLE_KEY)
            if key == row_key:
                self.selectionModel().setCurrentIndex(item, QItemSelectionModel.SelectCurrent | QItemSelectionModel.Rows)
                break

    def item_changed(self, idx):
        # TODO use siblingAtColumn when min Qt version is >=5.11
        item = self.model().itemFromIndex(idx.sibling(idx.row(), self.Columns.DATE))
        request_type = item.data(ROLE_REQUEST_TYPE)
        key = item.data(ROLE_KEY)
        req = self.wallet.get_request(key)
        if req is None:
            self.update()
            return
        is_lightning = request_type == PR_TYPE_LN
        text = req.get('invoice') if is_lightning else req.get('URI')
        self.parent.receive_address_e.setText(text)

    def refresh_status(self):
        m = self.model()
        for r in range(m.rowCount()):
            idx = m.index(r, self.Columns.STATUS)
            date_idx = idx.sibling(idx.row(), self.Columns.DATE)
            date_item = m.itemFromIndex(date_idx)
            status_item = m.itemFromIndex(idx)
            key = date_item.data(ROLE_KEY)
            is_lightning = date_item.data(ROLE_REQUEST_TYPE) == PR_TYPE_LN
            req = self.wallet.get_request(key)
            if req:
                status = req['status']
                status_str = get_request_status(req)
                status_item.setText(status_str)
                status_item.setIcon(read_QIcon(pr_icons.get(status)))

    def update(self):
        self.wallet = self.parent.wallet
        domain = self.wallet.get_receiving_addresses()
        self.parent.update_receive_address_styling()
        self.model().clear()
        self.update_headers(self.__class__.headers)
        for req in self.wallet.get_sorted_requests(self.config):
            status = req.get('status')
            if status == PR_PAID:
                continue
            is_lightning = req['type'] == PR_TYPE_LN
            request_type = req['type']
            timestamp = req.get('time', 0)
            amount = req.get('amount')
            message = req['message'] if is_lightning else req['memo']
            date = format_time(timestamp)
            amount_str = self.parent.format_amount(amount) if amount else ""
            status_str = get_request_status(req)
            labels = [date, message, amount_str, status_str]
            items = [QStandardItem(e) for e in labels]
            self.set_editability(items)
            items[self.Columns.DATE].setData(request_type, ROLE_REQUEST_TYPE)
            items[self.Columns.STATUS].setIcon(read_QIcon(pr_icons.get(status)))
            if request_type == PR_TYPE_LN:
                items[self.Columns.DATE].setData(req['rhash'], ROLE_KEY)
                items[self.Columns.DATE].setIcon(read_QIcon("lightning.png"))
            elif request_type == PR_TYPE_ADDRESS:
                address = req['address']
                if address not in domain:
                    continue
                expiration = req.get('exp', None)
                signature = req.get('sig')
                requestor = req.get('name', '')
                items[self.Columns.DATE].setData(address, ROLE_KEY)
                if signature is not None:
                    items[self.Columns.DATE].setIcon(read_QIcon("seal.png"))
                    items[self.Columns.DATE].setToolTip(f'signed by {requestor}')
                else:
                    items[self.Columns.DATE].setIcon(read_QIcon("bitcoin.png"))
            self.model().insertRow(self.model().rowCount(), items)
        self.filter()
        # sort requests by date
        self.model().sort(self.Columns.DATE)
        # hide list if empty
        if self.parent.isVisible():
            b = self.model().rowCount() > 0
            self.setVisible(b)
            self.parent.receive_requests_label.setVisible(b)

    def create_menu(self, position):
        idx = self.indexAt(position)
        item = self.model().itemFromIndex(idx)
        # TODO use siblingAtColumn when min Qt version is >=5.11
        item = self.model().itemFromIndex(idx.sibling(idx.row(), self.Columns.DATE))
        if not item:
            return
        key = item.data(ROLE_KEY)
        request_type = item.data(ROLE_REQUEST_TYPE)
        req = self.wallet.get_request(key)
        if req is None:
            self.update()
            return
        column = idx.column()
        column_title = self.model().horizontalHeaderItem(column).text()
        column_data = self.model().itemFromIndex(idx).text()
        menu = QMenu(self)
        if column == self.Columns.AMOUNT:
            column_data = column_data.strip()
        menu.addAction(_("Copy {}").format(column_title), lambda: self.parent.do_copy(column_title, column_data))
        if request_type == PR_TYPE_ADDRESS:
            menu.addAction(_("Copy Address"), lambda: self.parent.do_copy('Address', key))
        if request_type == PR_TYPE_LN:
            menu.addAction(_("Copy lightning payment request"), lambda: self.parent.do_copy('Request', req['invoice']))
        else:
            menu.addAction(_("Copy URI"), lambda: self.parent.do_copy('URI', req['URI']))
        if 'http_url' in req:
            menu.addAction(_("View in web browser"), lambda: webopen(req['http_url']))
        # do bip70 only for browser access
        # so, each request should have an ID, regardless
        #menu.addAction(_("Save as BIP70 file"), lambda: self.parent.export_payment_request(addr))
        menu.addAction(_("Delete"), lambda: self.parent.delete_request(key))
        run_hook('receive_list_menu', menu, key)
        menu.exec_(self.viewport().mapToGlobal(position))
