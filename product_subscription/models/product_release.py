# -*- coding: utf-8 -*-
from openerp import models, fields, api, _

from openerp.exceptions import UserError


class ProductRelease(models.Model):
    _name = "product.release.list"

    @api.multi
    def _compute_picking_ids(self):
        for product_release in self:
            product_release.picking_ids = product_release.product_release_lines.mapped('picking') # noqa
            product_release.delivery_count = len(product_release.picking_ids)

    name = fields.Char(string="Name", readonly=True, copy=False)
    release_date = fields.Date(string='Product Release Date',
                               readonly=True,
                               required=True,
                               index=True,
                               states={'draft': [('readonly', False)]})
    create_date = fields.Date(string='Creation Date',
                              readonly=True,
                              copy=False,
                              default=fields.Datetime.now,
                              help="Date on which product release list"
                              " is created.")

    user_id = fields.Many2one('res.users',
                              string='Release responsible',
                              copy=False,
                              default=lambda self: self.env.user)
    product_id = fields.Many2one('product.product',
                                 string='Product',
                                 domain=[('sale_ok', '=', True)],
                                 required=True,
                                 readonly=True,
                                 states={'draft': [('readonly', False)]})
    product_release_lines = fields.One2many('product.release.line',
                                            'product_release_list',
                                            string="Product release lines",
                                            copy=False)
    release_qty = fields.Integer(string="Product release quantity",
                                 required=True,
                                 default=1)

    state = fields.Selection([('draft', 'Draft'),
                              ('validated', 'Validated'),
                              ('done', 'Done'),
                              ('transfered', 'Transfered'),
                              ('cancelled', 'Cancelled'),
                              ],
                             string='State',
                             readonly=True,
                             copy=False,
                             default='draft')

    picking_policy = fields.Selection([
                            ('direct', 'Deliver each product when available'),
                            ('one', 'Deliver all products at once')],
                            string='Shipping Policy',
                            required=True,
                            readonly=True,
                            default='direct',
                            states={'draft': [('readonly', False)],
                                    'sent': [('readonly', False)]})
    picking_ids = fields.Many2many('stock.picking',
                                   compute='_compute_picking_ids',
                                   string='Picking associated to this release')
    delivery_count = fields.Integer(string='Delivery Orders',
                                    compute='_compute_picking_ids')

    @api.multi
    def action_view_delivery(self):
        '''
        This function returns an action that display existing delivery orders
        of given sales order ids. It can either be a in a list or in a form
        view, if there is only one delivery order to show.
        '''
        action = self.env.ref('stock.action_picking_tree_all')

        result = {
            'name': action.name,
            'help': action.help,
            'type': action.type,
            'view_type': action.view_type,
            'view_mode': action.view_mode,
            'target': action.target,
            'context': action.context,
            'res_model': action.res_model,
        }

        pick_ids = sum([order.picking_ids.ids for order in self], [])

        if len(pick_ids) > 1:
            result['domain'] = "[('id','in',["+','.join(map(str, pick_ids))+"])]" # noqa
        elif len(pick_ids) == 1:
            form = self.env.ref('stock.view_picking_form', False)
            form_id = form.id if form else False
            result['views'] = [(form_id, 'form')]
            result['res_id'] = pick_ids[0]
        return result

    @api.one
    def action_draft(self):
        self.product_release_lines.unlink()
        self.state = 'draft'

    @api.one
    def action_cancel(self):
        self.product_release_lines.unlink()
        if self.state in ['draft', 'validated']:
            self.state = 'cancelled'

    @api.one
    def action_validate(self):
        prod_sub_obj = self.env['product.subscription.object']
        release_line_obj = self.env['product.release.line']

        vals = {'state': 'validated'}
        if self.name == '' or not self.name:
            prod_rel_seq = self.env.ref('product_subscription.sequence_product_release', False) # noqa
            vals['name'] = prod_rel_seq.next_by_id()

        subscriptions = prod_sub_obj.search([('counter', '>', 0)])

        line_vals = {'product_release_list': self.id,
                     'product_id': self.product_id.id}

        for subscription in subscriptions:
            line_vals['subscriber'] = subscription.subscriber.id
            line_vals['product_subscription'] = subscription.id
            release_line_obj.create(line_vals)
        self.write(vals)

    def get_picking_vals(self, picking_type):
        return {'picking_type_code': 'outgoing',
                'origin': self.name,
                'move_type': self.picking_policy,
                'picking_type_id': picking_type.id,
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': picking_type.default_location_dest_id.id}

    def get_stock_move_vals(self, picking_type):
        return {'name': '/',
                'product_id': self.product_id.id,
                'product_uom': self.product_id.uom_id.id,
                'product_uom_qty': self.release_qty,
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': picking_type.default_location_dest_id.id}

    @api.one
    def action_done(self):
        picking_type_obj = self.env['stock.picking.type']
        picking_type = picking_type_obj.search([('code', '=', 'outgoing')])
        stock_move_vals = self.get_stock_move_vals(picking_type)
        picking_vals = self.get_picking_vals(picking_type)

        for line in self.product_release_lines:
            vals = dict(picking_vals)
            if line.product_subscription.counter - self.release_qty >= 0:
                line.create_picking(vals, stock_move_vals)
                line.product_subscription.counter = line.product_subscription.counter - self.release_qty # noqa

        subs_terminated = self.product_release_lines.filtered(lambda record: record.product_subscription.counter == 0) # noqa
        subs_renew = self.product_release_lines.filtered(lambda record: record.product_subscription.counter == 1) # noqa

        subs_terminated.write({'state': 'terminated'})
        subscriber_terminated = subs_terminated.mapped('subscriber')
        subscriber_terminated.write({
                                'subscriber': False,
                                'old_subscriber': True
                                })
        subs_renew.write({'state': 'renew'})
        self.state = 'done'

        return True

    @api.one
    def action_transfert(self):
        for picking in self.product_release_lines.mapped('picking'):
            if picking.state not in ['cancel', 'done']:
                if picking.state != 'assigned':
                    picking.recheck_availability()
                    if picking.state != 'assigned':
                        raise UserError(_("Not enough stock to deliver! Please"
                                          " check that there is sufficient"
                                          " product available"))
                for pack_operation in picking.pack_operation_ids:
                    if pack_operation.product_id.id == self.product_id.id:
                        pack_operation.qty_done = self.release_qty
                picking.do_transfer()
        self.state = 'transfered'

        return True


class ProductReleaseLine(models.Model):
    _name = "product.release.line"

    product_release_list = fields.Many2one('product.release.list',
                                           string="Product release list",
                                           required=True)
    subscriber = fields.Many2one('res.partner',
                                 string="Subscriber",
                                 domain=[('subscriber', '=', True)],
                                 required=True)
    product_id = fields.Many2one('product.product',
                                 string='Product',
                                 required=True)
    product_subscription = fields.Many2one('product.subscription.object',
                                           string='Subscription',
                                           required=True)
    counter = fields.Float(related='product_subscription.counter',
                           string="Counteur",
                           readonly=True)
    picking = fields.Many2one('stock.picking',
                              string="Delivery order")

    @api.model
    def create_picking(self, vals, stock_move_vals):
        picking_obj = self.env['stock.picking']
        stock_move_obj = self.env['stock.move']

        vals['partner_id'] = self.subscriber.id

        picking = picking_obj.create(vals)
        stock_move_vals['picking_id'] = picking.id
        stock_move_obj.create(stock_move_vals)

        self.picking = picking

        return picking
