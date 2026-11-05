package query_types;
  typedef struct packed {
    logic [1:0] tag;
    logic       valid;
  } payload_t;
endpackage

module query_leaf #(
  parameter int WIDTH = 4,
  parameter int DEPTH = 2
) (
  input  logic                 clk,
  input  logic                 rst_n,
  input  logic                 enable,
  input  logic [WIDTH-1:0]     data_i,
  output logic [WIDTH-1:0]     data_q
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      data_q <= '0;
    else if (enable)
      data_q <= data_i;
  end
endmodule

module connection_leaf (
  input  logic defaulted_i = 1'b1,
  output logic open_o,
  output logic missing_o
);
  assign open_o = defaulted_i;
  assign missing_o = defaulted_i;
endmodule

module wildcard_leaf (
  input  logic wildcard_i,
  output logic wildcard_o
);
  assign wildcard_o = wildcard_i;
endmodule

module query_top #(
  parameter bit MAKE_EXTRA = 1'b1
) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       enable,
  input  logic [3:0] left_i,
  input  logic [3:0] right_i,
  output logic [3:0] result_o
);
  import query_types::*;

  payload_t status;
  logic [3:0] leaf_q;
  logic       duplicate;
  logic       wildcard_i;
  logic       wildcard_o;

  query_leaf #(.WIDTH(4)) u_leaf (
    .clk,
    .rst_n,
    .enable,
    .data_i ({left_i[1:0], right_i[1:0]}),
    .data_q (leaf_q)
  );

  connection_leaf u_connections (
    .open_o ()
  );

  wildcard_leaf u_wildcard (.*);

  if (MAKE_EXTRA) begin : g_extra
    logic duplicate;
    assign duplicate = enable;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      status <= '0;
    end else if (enable) begin
      status.tag <= leaf_q[1:0];
      status.valid <= 1'b1;
    end
  end

  assign duplicate = status.valid;
  assign result_o = leaf_q;
endmodule
