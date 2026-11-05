module fixture_leaf #(
  parameter int WIDTH = 4
) (
  input  logic [WIDTH-1:0] data_i,
  input  logic             enable_i,
  output logic [WIDTH-1:0] data_o
);
  assign data_o = enable_i ? data_i : '0;
endmodule
