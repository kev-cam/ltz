#!/usr/bin/env perl
#
# raw2ltraw.pl <xyce.raw> <title_path> — re-spell a Xyce rawfile (ASCII or
# binary) as an LTspice ASCII .raw so the LTspice waveform viewer opens it
# (File -> Open, or alongside its schematic). Mirrors the header LTspice
# itself writes with -ascii: Offset line, Command identity, lowercase time.
#
use strict;
use warnings;
use POSIX qw(strftime);
binmode(STDOUT, ':crlf');   # LTspice's reader expects CRLF line endings
use lib '/usr/local/src/sv2ghdl/regress/lib';
use Regress::RawCompare qw(read_raw);

my ($xraw, $title) = @ARGV;
die "usage: $0 xyce.raw title_path\n" unless $xraw && $title;

my $r = read_raw($xraw) or die "unparsable Xyce raw: $xraw\n";
my @names = @{ $r->{names} };
my @rows  = @{ $r->{rows} };
my $nv = scalar @names;
my $np = scalar @rows;

# LTspice spellings: nodes -> V(node), branch currents -> I(src)
my @lt;
for my $i (0 .. $#names) {
    my $n = $names[$i];
    if    ($n =~ /^(?:time|sweep|frequency)$/i) { push @lt, lc $n; }
    elsif ($n =~ /^(\w+)#branch$/i)             { push @lt, 'I(' . uc($1) . ')'; }
    else                                        { push @lt, 'V(' . lc($n) . ')'; }
}

my $plot = 'Transient Analysis';
$plot = 'AC Analysis'                 if $lt[0] eq 'frequency';
$plot = 'DC transfer characteristic'  if $lt[0] eq 'sweep';

print "Title: $title\n";
print 'Date: ' . strftime('%a %b %e %H:%M:%S %Y', localtime) . "\n";
print "Plotname: $plot\n";
print "Flags: real\n";
print "No. Variables: $nv\n";
printf "No. Points: %12d\n", $np;
print "Offset:    0.0000000000000000e+00\n";
print "Command: Linear Technology Corporation LTspice (via ltwatch/Xyce)\n";
print "Variables:\n";
for my $i (0 .. $#lt) {
    my $t = $i == 0 ? ($lt[0] eq 'frequency' ? 'frequency' : 'time')
                    : ($lt[$i] =~ /^I\(/ ? 'device_current' : 'voltage');
    print "\t$i\t$lt[$i]\t$t\n";
}
print "Values:\n";
for my $p (0 .. $np - 1) {
    for my $v (0 .. $nv - 1) {
        my $val = sprintf('%.15e', $rows[$p][$v]);
        print "$p\t$val\n" if $v == 0;
        print "\t$val\n"   if $v > 0;
    }
}
